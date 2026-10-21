"""bore kipi: ekzhang/bore protokolünü konuşan sahte bir sunucuya karşı Host + Client e2e (hesapsız genel tünel).

Gerçek bore.pub'a dokunmaz. Sahte sunucu, bore kaynak kodunda (shared.rs, client.rs, server.rs) okunan davranışı taklit eder:
kontrol portu, "\\0" sonlu JSON; Hello / Heartbeat / Connection / Accept / Error / Challenge."""
import json
import socket
import threading
import time
import uuid

import pytest

from test_e2e import event_calls, relays, rig, sandbox, until  # noqa: F401  (fixture'lar buradan gelir)
from uzakmasa import host as hostmod


def _frame(conn, buf):
    while b"\0" not in buf:
        chunk = conn.recv(256)
        if not chunk:
            raise ConnectionError("kapandı")
        buf += chunk
    frame, _, rest = bytes(buf).partition(b"\0")
    buf[:] = rest
    return json.loads(frame)


def _send(conn, msg):
    conn.sendall(json.dumps(msg, separators=(",", ":")).encode() + b"\0")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeBore:
    """Kontrol portunda Hello/Accept bekler; Hello'ya genel bir loopback portu açar, ziyaretçiyi Accept ile boruya bağlar."""

    def __init__(self, challenge=False, taken=()):
        self.challenge, self.taken = challenge, set(taken)
        self.hellos, self.freed, self.pending, self.socks = [], [], {}, []
        self.dead = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.dead = True
        self.drop()
        self.listener.close()

    def drop(self):
        """Sunucu yeniden başladı gibi: bütün kontrol ve genel soketleri kapat."""
        for s in list(self.socks):
            try:
                s.close()
            except OSError:
                pass

    def _accept(self):
        while not self.dead:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.socks.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf, pub = bytearray(), None
        try:
            if self.challenge:
                _send(conn, {"Challenge": str(uuid.uuid4())})
                _frame(conn, buf)  # istemci kapatana dek bekle
                return
            msg = _frame(conn, buf)
            if "Accept" in msg:
                owner, conn = conn, None  # borunun sahibi artık pompalar
                self._pipe(msg["Accept"], owner, buf)
                return
            want = msg["Hello"]
            self.hellos.append(want)
            if want and want in self.taken:
                _send(conn, {"Error": "port already in use"})
                return
            pub = socket.socket()
            self.socks.append(pub)
            pub.bind(("127.0.0.1", want))
            pub.listen(8)
            pub.settimeout(0.1)
            port = pub.getsockname()[1]
            _send(conn, {"Hello": port})
            while not self.dead:
                try:
                    visitor, _ = pub.accept()
                except socket.timeout:
                    _send(conn, "Heartbeat")  # gerçek sunucu da düz metin "Heartbeat" yollar
                    continue
                cid = str(uuid.uuid4())
                self.pending[cid] = visitor
                _send(conn, {"Connection": cid})
        except (OSError, ValueError, KeyError, ConnectionError):
            pass
        finally:
            if pub is not None:
                self.freed.append(pub.getsockname()[1] if pub.fileno() != -1 else "kapalı")
                pub.close()
            if conn is not None:
                conn.close()

    def _pipe(self, cid, conn, leftover):
        visitor = self.pending.pop(cid, None)
        if visitor is None:
            conn.close()
            return
        if leftover:
            visitor.sendall(bytes(leftover))

        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.close()
                    except OSError:
                        pass

        threading.Thread(target=pump, args=(visitor, conn), daemon=True).start()
        threading.Thread(target=pump, args=(conn, visitor), daemon=True).start()


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """Hiçbir bore testi gerçek ağa (bore.pub) çıkamasın."""
    real = socket.create_connection

    def guarded(address, *a, **k):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise AssertionError("test gerçek ağa çıkmamalı: %r" % (address,))
        return real(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", guarded)


@pytest.fixture
def bore():
    made = []

    def make(**kw):
        made.append(FakeBore(**kw))
        return made[-1]

    yield make
    for b in made:
        b.close()


def cfg_for(fb, **over):
    return {"bore_server": "127.0.0.1:%d" % fb.port, **over}


def test_full_session_through_bore_with_the_public_port_as_id(bore, rig):
    fb = bore()
    r = rig("bore", **cfg_for(fb))
    cid = r.start()
    host, _, port = cid.rpartition(":")
    assert host == "127.0.0.1" and int(port) != fb.port  # ID = sunucu:GENEL port, kontrol portu değil
    assert fb.hellos == [0]
    c, cu = r.connected_client()  # istemci ID'yi olduğu gibi yazar: ":" içerdiği için doğrudan bağlanır
    c.send_input({"t": "c", "c": "ş"})
    until(lambda: event_calls(r) == [("char", "ş")], 5, "girdi tünelden geçti")
    assert r.ui.of("consent"), "ilk eşleşme onay istemeli"
    assert "hata" not in " ".join(r.ui.texts()).lower()


def test_asks_for_the_last_public_port_again_and_remembers_it(bore, rig):
    fb = bore()
    p = free_port()
    r = rig("bore", **cfg_for(fb, bore_port=p))
    cid = r.start()
    assert fb.hellos == [p] and cid == "127.0.0.1:%d" % p  # ID kararlı kaldı
    assert r.saved and r.saved[-1]["bore_port"] == p


def test_falls_back_to_a_random_port_when_the_old_one_is_taken(bore, rig):
    p = free_port()
    fb = bore(taken=[p])
    r = rig("bore", **cfg_for(fb, bore_port=p))
    cid = r.start()
    assert fb.hellos == [p, 0] and cid != "127.0.0.1:%d" % p
    assert any("yeni port" in t for t in r.ui.texts())
    assert r.saved[-1]["bore_port"] == int(cid.rpartition(":")[2])


def test_control_drop_reconnects_and_keeps_the_same_public_port(bore, rig, monkeypatch):
    monkeypatch.setattr(hostmod, "RETRY_MIN", 0.1)
    fb = bore()
    r = rig("bore", **cfg_for(fb))
    cid = r.start()
    fb.drop()  # sunucu yeniden başladı
    until(lambda: len(r.ui.of("ready")) >= 2, 8, "yeniden hazır")
    assert r.ui.of("ready")[-1] == (cid,) and fb.hellos[1:] == [int(cid.rpartition(":")[2])]
    c, cu = r.connected_client()
    assert cu.frames


def test_server_that_wants_a_secret_is_reported_and_not_retried(bore, rig):
    fb = bore(challenge=True)
    r = rig("bore", **cfg_for(fb))
    r.host.start()
    until(lambda: r.ui.of("error"), 5, "hata bildirimi")
    assert "gizli anahtar" in r.ui.of("error")[0][0]


def test_stop_closes_the_control_connection_at_once(bore, rig):
    fb = bore()
    r = rig("bore", **cfg_for(fb))
    r.start()
    t0 = time.monotonic()
    r.host.stop()
    until(lambda: fb.freed, 3, "genel port serbest kaldı")
    assert time.monotonic() - t0 < 3


@pytest.mark.parametrize("bad", ["sunucu:abc", "sunucu:0", "sunucu:70000", ":7835"])
def test_server_setting_is_validated_before_dialing(bore, rig, bad):
    r = rig("bore", bore_server=bad)
    r.host.start()
    until(lambda: r.ui.of("error"), 3, "hata bildirimi")
    assert "biçiminde" in r.ui.of("error")[0][0]
