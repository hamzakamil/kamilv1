"""Host + Client motorları: loopback e2e (SPEC kabul C) ve gerçek relay üzerinden e2e (kabul D).

Yalnız sahte nesneler: kareyi testin yönettiği sahte yakalayıcı, kayıt tutan sahte enjektör, gerçek yerel dinleyiciye
yönlendiren sahte ngrok. Hiçbir test gerçek girdi göndermez ya da ngrok'a dokunmaz; her bekleyişin kendi süre sınırı var."""
import asyncio
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from PIL import Image
from pyngrok import ngrok as real_ngrok

import relay
from uzakmasa import client as clientmod
from uzakmasa import config, proto, winio
from uzakmasa import host as hostmod

ROOT = Path(__file__).resolve().parents[1]
MAIN = threading.get_ident()
PASSWORD = "deneme şifresi 0123456789"
WRONG = "yanlış şifre denemesi 9876"
KEY = proto.derive_key(PASSWORD)  # scrypt ~80 ms: modül başına bir kez
UID, UID2 = "a1" * 16, "b2" * 16
NGROK_TOKEN = "tok-AbCdEf123456"
RELAY_TOKEN = "gizli röle tokenı"  # boşluk ve Türkçe harf: ön yazı ve utf-8 yolu da sınansın
RELAY_ID = "123456789"


def sha(uid):
    return hashlib.sha256(uid.encode()).hexdigest()


def until(cond, timeout=5.0, what="koşul"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    assert cond(), "%s %.1f sn içinde gerçekleşmedi" % (what, timeout)


def tripwire(name):
    def boom(*a, **k):
        raise AssertionError("%s çağrıldı: test sahte nesneyle çalışmalıydı" % name)

    return boom


# --- sahte nesneler -----------------------------------------------------------------


class HostUI:
    """Host'un ui geri çağrıları: hepsi çağıran iş parçacığıyla birlikte kaydedilir."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls, self.threads = [], set()
        self.consent_answer = True  # bool ya da (ad, adres) -> bool
        self.ready_evt = threading.Event()

    def _rec(self, name, *args):
        with self.lock:
            self.calls.append((name, args))
            self.threads.add(threading.get_ident())

    def of(self, name):
        with self.lock:
            return [a for n, a in self.calls if n == name]

    def texts(self):
        return [a[0] for n, a in list(self.calls) if n in ("log", "error")]

    def log(self, text):
        self._rec("log", text)

    def ready(self, conn_id):
        self._rec("ready", conn_id)
        self.ready_evt.set()

    def error(self, text):
        self._rec("error", text)

    def connected(self, name, addr):
        self._rec("connected", name, addr)

    def disconnected(self):
        self._rec("disconnected")

    def stats(self, total):
        self._rec("stats", total)

    def consent(self, name, addr):
        self._rec("consent", name, addr)
        a = self.consent_answer
        return a(name, addr) if callable(a) else a


class ClientUI:
    def __init__(self):
        self.lock = threading.Lock()
        self.statuses, self.frames, self.closings, self.threads = [], [], [], set()
        self.frame_delay = 0.0  # yavaş okuyucu benzetimi

    def status(self, text):
        with self.lock:
            self.statuses.append((time.monotonic(), text))
            self.threads.add(threading.get_ident())

    def frame(self, img):
        with self.lock:
            self.frames.append(img.width)  # sahte yakalayıcı kare numarasını genişliğe yazar
            self.threads.add(threading.get_ident())
        time.sleep(self.frame_delay)

    def closed(self, code, text, fatal):
        with self.lock:
            self.closings.append((time.monotonic(), code, text, fatal))
            self.threads.add(threading.get_ident())

    @property
    def final(self):
        return next((c for c in list(self.closings) if c[3]), None)

    def texts(self):
        return [s for _, s in list(self.statuses)]


class FakeScreen:
    """Yakalayıcı fabrikası: kareyi test yönetir. Genişlik = 100 + n (JPEG boyutu korur), anahtar n'ye bağlı."""

    def __init__(self):
        self.n = 0
        self.auto = False  # True: her yakalamada ekran değişir (sürekli hareket)
        self.noise = False  # True: ~380 KB'lık gürültülü kareler
        self.fail = 0  # sonraki bu kadar yakalama fırlatsın
        self.calls = self.made = 0
        self.threads = set()
        self._noise_img = None

    def change(self):
        self.n += 1

    def factory(self):
        self.made += 1
        self.threads.add(threading.get_ident())
        return self

    def grab(self):
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.fail:
            self.fail -= 1
            raise RuntimeError("yakalama hatası")
        if self.auto:
            self.n += 1
        n = self.n
        if self.noise:
            self._noise_img = self._noise_img or Image.frombytes("RGB", (1280, 720), os.urandom(1280 * 720 * 3))
            return self._noise_img, b"key-%d" % n
        return Image.new("RGB", (100 + n, 60), (n % 256, 90, 160)), b"key-%d" % n


class RecInjector:
    def __init__(self):
        self.calls, self.threads = [], set()
        self.on_char = None

    def _rec(self, *call):
        self.calls.append(call)
        self.threads.add(threading.get_ident())

    def move(self, nx, ny):
        self._rec("move", nx, ny)

    def button(self, b, down, nx=None, ny=None):
        self._rec("button", b, down, nx, ny)

    def wheel(self, delta):
        self._rec("wheel", delta)

    def char(self, ch):
        self._rec("char", ch)
        if self.on_char:
            self.on_char(ch)

    def key(self, vk, down):
        self._rec("key", vk, down)

    def release_all(self):
        self._rec("release_all")

    def without_release(self):
        return [c for c in list(self.calls) if c[0] != "release_all"]


class FakeNgrok:
    """pyngrok.ngrok yerine: çağrıları kaydeder, 'tüneli' gerçek yerel dinleyiciye (ya da araya giren aktarıcıya) yönlendirir."""

    def __init__(self):
        self.calls = []
        self.connect_gate = None  # Event: connect bunu bekler
        self.kill_gate = None  # Event: ikinci ve sonraki kill'ler bunu bekler
        self.fail = None  # connect'te fırlatılacak istisna
        self.use_proxy = False
        self.proxies = []

    def names(self):
        return [c[0] for c in list(self.calls)]

    def kill(self, pyngrok_config=None):
        self.calls.append(("kill", pyngrok_config))
        if self.kill_gate is not None and self.names().count("kill") > 1:
            self.kill_gate.wait(10)

    def connect(self, **kw):
        self.calls.append(("connect", kw))
        if self.connect_gate is not None:
            self.connect_gate.wait(10)
        if self.fail is not None:
            raise self.fail
        upstream = kw["addr"].split("://", 1)[1]
        if self.use_proxy:
            self.proxies.append(Proxy(upstream))
            upstream = self.proxies[-1].addr
        return types.SimpleNamespace(public_url="tcp://" + upstream)


class Proxy:
    """Araya giren TCP aktarıcı. blackhole(): mevcut bağlantılar sessizce yutulur (yarı açık kopma), yenileri normal geçer."""

    def __init__(self, upstream):
        h, _, port = upstream.rpartition(":")
        self.up = (h, int(port))
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.srv.settimeout(0.3)
        self.addr = "127.0.0.1:%d" % self.srv.getsockname()[1]
        self.pipes, self.stopped = [], False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped:
            try:
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                u = socket.create_connection(self.up, 5)
            except OSError:
                c.close()
                continue
            pipe = {"drop": False, "socks": (c, u)}
            self.pipes.append(pipe)
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self._pump, args=(a, b, pipe), daemon=True).start()

    @staticmethod
    def _pump(a, b, pipe):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    if not pipe["drop"]:
                        b.close()  # normal kapanış karşıya geçer; yutulan bağlantıda geçmez: öbür taraf sessizce açık kalır
                    return
                if not pipe["drop"]:
                    b.sendall(data)
        except OSError:
            pass

    def blackhole(self):
        for p in self.pipes:
            p["drop"] = True

    def close(self):
        self.stopped = True
        for p in self.pipes:
            for s in p["socks"]:
                s.close()
        self.srv.close()


class RelayRunner:
    """relay.py sunucusu bu süreçte 127.0.0.1'de, kendi olay döngüsüyle arka plan iş parçacığında."""

    def __init__(self, token=RELAY_TOKEN, port=0):
        self.relay = relay.Relay(token)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        try:
            self.port = self.call(self.relay.start("127.0.0.1", port))
        except BaseException:
            self.close_loop()
            raise

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def close_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def stop(self):
        try:
            self.call(self.relay.stop())
        finally:
            self.close_loop()


class Raw:
    """Ham istemci: gerçek el sıkışma + H; ready=True ise W beklenir. Kare/ack akışı elle yürütülür."""

    def __init__(self, conn_id, key=KEY, uid=UID, ready=True, name="ham-istemci"):
        h, _, port = conn_id.rpartition(":")
        self.sock = socket.create_connection((h, int(port)), 5)
        self.ch = proto.client_handshake(self.sock, conn_id, key)
        self.ch.send(b"H", json.dumps({"uid": uid, "name": name, "v": 1}).encode())
        self.sock.settimeout(5)
        if ready:
            kind, _ = self.ch.recv()
            assert kind == b"W"
            self.ch.trust()

    def next(self, timeout=3.0):
        """(tür, yük) ya da süre dolarsa None. Çerçeveler küçük ve tek parça gelir: ortada zaman aşımı yok."""
        self.sock.settimeout(timeout)
        try:
            return self.ch.recv()
        except socket.timeout:
            return None

    def frame(self, timeout=3.0, ack=True):
        """Sıradaki F karesinin genişliği (P'ler atlanır); ack=True ise A yollar."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            got = self.next(left)
            if got and got[0] == b"F":
                if ack:
                    self.ch.send(b"A")
                return Image.open(io.BytesIO(got[1])).width
        raise AssertionError("%.1f sn içinde kare gelmedi" % timeout)

    def drain(self, seconds):
        """seconds boyunca gelen her şeyi toplar: [(tür, yük)]."""
        out, end = [], time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            got = self.next(left)
            if got:
                out.append(got)
        return out

    def event(self, ev):
        self.ch.send(b"I", json.dumps(ev).encode())

    def close(self):
        ch = getattr(self, "ch", None)  # Raw.__new__ ile elle kurulan bazı testler ch'yi hiç atamaz
        (ch or self.sock).close()


class Rig:
    """Bir Host (ngrok ya da relay kipinde), ona bağlanan istemciler ve genel değişmezler."""

    def __init__(self, mode="ngrok", **cfg_over):
        self.cfg = {"k": KEY.hex(), "trusted": [], "ngrok_token": NGROK_TOKEN, "relay": "", "relay_token": "",
                    "relay_id": RELAY_ID}
        self.cfg.update(cfg_over)
        self.ui, self.screen, self.inj = HostUI(), FakeScreen(), RecInjector()
        self.saved = []
        self.host = hostmod.Host(self.cfg, self.ui, mode, save=lambda c: self.saved.append(json.loads(json.dumps(c))),
                                 grabber_factory=self.screen.factory, injector=self.inj, fail_delay=0.05)
        self.clients, self.raws, self.gates = [], [], []
        self.secrets = [PASSWORD, KEY.hex(), NGROK_TOKEN, UID, UID2, sha(UID), sha(UID2)]

    def start(self):
        self.host.start()
        assert self.ui.ready_evt.wait(8), "Host hazır olmadı"
        return self.host.conn_id

    def client(self, password=PASSWORD, uid=UID, conn_id=None, relay=None):
        cu = ClientUI()
        c = clientmod.Client(conn_id or self.host.conn_id, password, cu, uid, relay=relay)
        self.clients.append((c, cu))
        c.start()
        return c, cu

    def connected_client(self, **kw):
        c, cu = self.client(**kw)
        until(lambda: cu.frames, 8, "ilk kare")
        return c, cu

    def raw(self, **kw):
        r = Raw(self.host.conn_id, **kw)
        self.raws.append(r)
        return r

    def gate(self):
        g = threading.Event()
        self.gates.append(g)
        return g

    def sessions_over(self, n):
        until(lambda: len(self.ui.of("disconnected")) >= n, 8, "oturum bitişi")

    def shutdown(self):
        for g in self.gates:
            g.set()
        for r in self.raws:
            r.close()
        for c, _ in self.clients:
            c.stop()
        self.host.stop()
        for c, _ in self.clients:
            if c._thread is not None:
                c._thread.join(8)
                assert not c._thread.is_alive(), "istemci iş parçacığı bitmedi"

    def check(self):
        # genel değişmezler: ui çağrıları Tk (ana) iş parçacığından gelmez; sırlar log'a girmez
        assert MAIN not in self.ui.threads
        for _, cu in self.clients:
            assert MAIN not in cu.threads
        logs = "\n".join(self.ui.texts())
        for s in self.secrets:
            assert s not in logs, "sır log'a sızdı: %r" % s[:6]


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    state = types.SimpleNamespace(locked=False, awake=[], ngrok=FakeNgrok())
    monkeypatch.setattr(config, "APPDIR", str(tmp_path / "UzakMasa"))  # gerçek profile asla dokunma
    monkeypatch.setattr(hostmod, "ngrok", state.ngrok)
    for name in ("connect", "kill", "disconnect", "get_ngrok_process"):  # gerçek pyngrok yanlışlıkla çalışmasın
        monkeypatch.setattr(real_ngrok, name, tripwire("pyngrok.ngrok." + name))
    monkeypatch.setattr(winio, "_send", tripwire("SendInput"))  # gerçek girdi imkânsız
    monkeypatch.setattr(winio, "screen_locked", lambda: state.locked)
    monkeypatch.setattr(winio, "keep_awake", lambda on: state.awake.append((on, threading.get_ident())))
    monkeypatch.setattr(clientmod, "RETRY_DELAY", 0.3)  # varsayılan 3 sn'yi ölçen testler geri alır
    yield state
    for p in state.ngrok.proxies:
        p.close()


@pytest.fixture
def relays():
    made = []

    def make(token=RELAY_TOKEN, port=0):
        made.append(RelayRunner(token, port))
        return made[-1]

    yield make
    for rr in made:
        try:
            rr.stop()
        except Exception:  # testin kendisi durdurmuş olabilir
            pass


@pytest.fixture
def rig(sandbox, relays):
    made = []

    def make(mode="ngrok", **kw):
        made.append(Rig(mode, **kw))
        return made[-1]

    yield make
    for r in made:
        r.shutdown()

    def alive():
        return [t.name for t in threading.enumerate() if t.name.startswith(("host-", "client-"))]

    until(lambda: not alive(), 10, "iş parçacıklarının bitmesi")  # arkada hiçbir motor iş parçacığı kalmamalı
    for r in made:
        r.check()


def event_calls(rig_):
    return rig_.inj.without_release()


# --- kare akışı ---------------------------------------------------------------------


def test_frames_arrive_decoded_in_order_and_grabber_lives_in_streamer_thread(rig):
    r = rig()
    r.start()
    c, cu = r.client()
    until(lambda: cu.frames, 8, "ilk kare")
    for n in (1, 2, 3):
        r.screen.change()
        until(lambda: cu.frames[-1] == 100 + n, 5, "kare %d" % n)
    assert cu.frames == [100, 101, 102, 103]  # değişmeyen ekran tekrar kare üretmedi, sıra bozulmadı
    assert cu.texts()[:3] == ["Bağlanıyor…", "Host onayı bekleniyor…", "Bağlandı"]
    assert r.screen.made == 1 and len(r.screen.threads) == 1 and MAIN not in r.screen.threads
    ((name, addr),) = r.ui.of("connected")
    assert name and addr.startswith("127.0.0.1:")


def test_static_screen_still_gets_first_frame_on_every_connect(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    c.stop()
    r.sessions_over(1)
    c2, cu2 = r.connected_client()  # ekran hiç değişmedi: yine de ilk kare gelmeli
    assert cu.frames == cu2.frames == [100]


def test_idle_screen_sends_no_frames_only_keepalive_and_idle_poll_is_slow(rig, monkeypatch):
    # IDLE_POLL (0.25) döngü başına bir kez denetlenir: KEEPALIVE onunla aynı sıraya gelmeli yoksa
    # gerçek aralık IDLE_POLL'un katına yuvarlanır (0.3 -> ~0.5) ve 1.3 sn'de 2'ye düşer
    monkeypatch.setattr(hostmod, "KEEPALIVE", 0.15)
    r = rig()
    r.start()
    raw = r.raw()
    assert raw.frame() == 100
    calls0 = r.screen.calls
    got = raw.drain(1.3)
    assert got and all(k == b"P" and p == b"" for k, p in got) and len(got) >= 3  # kare yok, yalnız canlılık
    # boşta ~4 Hz: 12 Hz'lik sürekli yakalama CPU'yu boşuna yakardı
    assert 2 <= r.screen.calls - calls0 <= 8


def test_keepalive_after_three_seconds_of_silence_default_timing(rig):
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    t0 = time.monotonic()
    got = raw.next(6)
    assert got == (b"P", b"")
    assert 2.7 <= time.monotonic() - t0 <= 4.5  # SPEC: ≥3 sn sessizlikten sonra


def test_change_after_idle_is_sent_quickly_by_input_wake(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "IDLE_POLL", 3.0)  # uyandırma olmasa kare 3 sn sonra gelirdi
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    time.sleep(0.6)  # akıtıcı artık uzun idle beklemesinde
    r.screen.change()
    t0 = time.monotonic()
    raw.event({"t": "m", "x": 0.5, "y": 0.5})
    assert raw.frame() == 101
    assert time.monotonic() - t0 < 0.8  # SPEC: ~0.4 sn (yük altındaki makineye pay bırakıldı)


def test_change_without_input_is_picked_up_within_one_idle_poll(rig):
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    time.sleep(0.5)
    r.screen.change()
    t0 = time.monotonic()
    assert raw.frame() == 101
    assert time.monotonic() - t0 < 0.9  # IDLE_POLL 0.25 sn + yakalama


def test_echo_after_input_is_not_missed_when_screen_updates_late(rig, monkeypatch):
    # tuş yankısı uygulamada birkaç on ms sonra çizilir: girdinin hemen ardındaki tek yakalama onu görmez
    monkeypatch.setattr(hostmod, "IDLE_POLL", 3.0)
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    time.sleep(0.6)
    r.inj.on_char = lambda ch: threading.Timer(0.08, r.screen.change).start()
    t0 = time.monotonic()
    raw.event({"t": "c", "c": "a"})
    assert raw.frame() == 101
    assert time.monotonic() - t0 < 1.0  # patlama penceresi olmasa IDLE_POLL = 3 sn beklenirdi


def test_grab_errors_are_skipped_rate_limited_and_grabber_is_rebuilt(rig):
    r = rig()
    r.screen.fail = 3
    r.start()
    raw = r.raw()
    assert raw.frame(timeout=5) == 100  # ilk üç yakalama patladı, oturum yaşıyor
    logs = [t for t in r.ui.texts() if "yakalanamadı" in t]
    assert len(logs) == 1  # 10 sn'de en çok bir satır
    assert r.screen.made >= 2  # bozuk örnek atılıp yenisi kuruldu


def test_adaptive_quality_rises_with_fast_acks_and_falls_with_slow_ones(rig, monkeypatch):
    qs = []
    real = winio.to_jpeg
    monkeypatch.setattr(winio, "to_jpeg", lambda img, q, w=1280: (qs.append(q), real(img, q, w))[1])
    r = rig()
    r.screen.auto = True
    r.start()
    raw = r.raw()
    for _ in range(9):
        raw.frame()  # her kareye hemen ack: RTT < 100 ms
    assert qs[0] == 55 and max(qs) == 70 and qs == sorted(qs)  # 3'er artar, 70'te kalır
    del qs[:]
    for _ in range(6):
        time.sleep(0.35)  # yavaş ack: RTT > 250 ms
        raw.frame()
    assert min(qs) == 30 and qs[-1] == 30 and qs[0] > qs[-1]  # 10'ar düşer, 30'da kalır


def test_ack_window_allows_two_frames_in_flight_and_slides(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "ACK_STALL", 30)  # emniyet sıfırlaması bu testi bozmasın
    r = rig()
    r.screen.auto = True
    r.start()
    raw = r.raw()
    assert raw.frame(ack=False) == 101 and raw.frame(ack=False) == 102
    assert [k for k, _ in raw.drain(0.7) if k == b"F"] == []  # ack yok: üçüncü kare gitmez
    raw.ch.send(b"A")
    assert len([k for k, _ in raw.drain(0.7) if k == b"F"]) == 1  # bir ack = bir kare
    raw.ch.send(b"A")
    raw.ch.send(b"A")
    assert len([k for k, _ in raw.drain(0.7) if k == b"F"]) == 2
    assert [k for k, _ in raw.drain(0.5) if k == b"F"] == []


def test_ack_window_resets_when_client_never_acks(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "ACK_STALL", 0.5)
    r = rig()
    r.screen.auto = True
    r.start()
    raw = r.raw()
    raw.frame(ack=False)
    raw.frame(ack=False)
    assert raw.frame(ack=False, timeout=3) > 0  # ack hiç gelmese de akış kilitlenmez


def test_slow_reading_client_does_not_build_lag(rig):
    r = rig()
    r.screen.auto = True
    r.start()
    c, cu = r.client()
    cu.frame_delay = 0.15  # her kareyi 150 ms'de işleyen yavaş görüntüleyici
    until(lambda: len(cu.frames) >= 3, 8, "kareler")
    for _ in range(8):
        time.sleep(0.2)
        assert r.screen.n - (cu.frames[-1] - 100) <= 4  # Host görüntülenenin en çok birkaç kare ötesine geçer
    assert len(cu.frames) >= 6


def test_blocked_send_does_not_block_stop_and_close_wakes_it(rig):
    r = rig()
    r.screen.auto = r.screen.noise = True
    r.start()
    raw = r.raw()
    acker = threading.Event()

    def blind_acks():  # kareleri okumadan ack yollar: pencere açık kalır, Host'un send'i dolan tamponda bloklanır
        try:
            while not acker.is_set():
                raw.ch.send(b"A")
                time.sleep(0.01)
        except OSError:
            pass

    threading.Thread(target=blind_acks, daemon=True).start()
    try:
        seen = []

        def plateau():  # akıtıcı bloklandı: yakalama sayısı artmıyor
            seen.append(r.screen.calls)
            time.sleep(0.15)
            return len(seen) > 6 and len(set(seen[-6:])) == 1

        until(plateau, 8, "bloklu send")
        t0 = time.perf_counter()
        r.host.stop()
        assert time.perf_counter() - t0 < 0.25  # stop() bloke olmaz
        r.sessions_over(1)  # kapatma bloklu send'i uyandırdı, oturum temizlendi
    finally:
        acker.set()


# --- girdi ----------------------------------------------------------------------------


def test_input_events_reach_the_injector_in_order_with_exact_values(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    evs = [
        {"t": "c", "c": "ş"}, {"t": "c", "c": "€"}, {"t": "c", "c": "\U0001F600"},
        {"t": "k", "vk": 65, "d": 1}, {"t": "k", "vk": 65, "d": 0}, {"t": "k", "vk": 254, "d": 1},
        {"t": "b", "b": "l", "d": 1, "x": 0.25, "y": 0.75}, {"t": "b", "b": "l", "d": 0, "x": 0.25, "y": 0.75},
        {"t": "b", "b": "r", "d": 1, "x": 0, "y": 1}, {"t": "b", "b": "m", "d": 0},
        {"t": "w", "d": -8}, {"t": "w", "d": 120}, {"t": "w", "d": -1200}, {"t": "w", "d": 1},
        {"t": "m", "x": 0.5, "y": 0.125},
    ]
    for ev in evs:
        c.send_input(ev)
    until(lambda: len(event_calls(r)) == len(evs), 5, "girdi olayları")
    assert event_calls(r) == [
        ("char", "ş"), ("char", "€"), ("char", "\U0001F600"),
        ("key", 65, True), ("key", 65, False), ("key", 254, True),
        ("button", "l", True, 0.25, 0.75), ("button", "l", False, 0.25, 0.75),
        ("button", "r", True, 0.0, 1.0), ("button", "m", False, None, None),
        ("wheel", -8), ("wheel", 120), ("wheel", -1200), ("wheel", 1),  # 120'nin katı olmayan küçük değerler de geçer
        ("move", 0.5, 0.125),
    ]
    assert len(r.inj.threads) == 1 and MAIN not in r.inj.threads  # tek iş parçacığı (oturum), Tk değil


@pytest.mark.parametrize("payload", [
    b"json degil", b"[]", b"{}", b'{"t":"m"}', b'{"t":"m","x":"a","y":1}', b'{"t":"m","x":NaN,"y":0}',
    b'{"t":"m","x":1e999,"y":0}', b'{"t":"m","x":true,"y":0}', b'{"t":"m","x":0.5}',
    b'{"t":"b","b":"x","d":1,"x":0,"y":0}', b'{"t":"b","b":"l","d":2}', b'{"t":"b","b":"l","d":true}',
    b'{"t":"b","b":"l","d":1,"x":0.5}',
    b'{"t":"w","d":0}', b'{"t":"w","d":1201}', b'{"t":"w","d":-1201}', b'{"t":"w","d":12.5}', b'{"t":"w","d":"120"}',
    b'{"t":"c","c":"ab"}', b'{"t":"c","c":""}', b'{"t":"c","c":5}',
    b'{"t":"k","vk":0,"d":1}', b'{"t":"k","vk":255,"d":1}', b'{"t":"k","vk":65.0,"d":1}', b'{"t":"k","vk":65,"d":3}',
    b'{"t":"z"}', b'{"t":null}', b"\xff\xfe\x00", b"[" * 2000, b"x" * 600,
])
def test_malformed_input_is_ignored_and_session_survives(rig, payload):
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    raw.ch.send(b"I", payload)
    raw.event({"t": "c", "c": "x"})  # arkasından gelen geçerli olay yine de uygulanır
    until(lambda: event_calls(r), 5, "geçerli olay")
    assert event_calls(r) == [("char", "x")]
    r.screen.change()
    assert raw.frame() == 101  # oturum hâlâ canlı


def test_unclamped_coordinates_pass_through_to_the_injector_which_clamps(rig):
    # Host yalnız sonlu sayı doğrular; [0,1] dışını Injector sıkıştırır (test_winio: test_move_is_absolute_and_clamped)
    r = rig()
    r.start()
    raw = r.raw()
    raw.event({"t": "m", "x": -5, "y": 9})
    until(lambda: event_calls(r), 5, "olay")
    assert event_calls(r) == [("move", -5.0, 9.0)]


def test_real_injector_behind_host_gets_correct_structs_without_real_input(rig, monkeypatch):
    sent = []
    monkeypatch.setattr(winio, "_send", lambda inputs: sent.append(
        [("k", i.ki.wVk, i.ki.wScan, i.ki.dwFlags) if i.type == 1 else ("m", i.mi.dx, i.mi.dy, i.mi.mouseData, i.mi.dwFlags)
         for i in inputs]))  # SendInput yerine kayıt: hiçbir şey sisteme gitmez
    r = rig()
    r.host.injector = winio.Injector()
    r.start()
    raw = r.raw()
    raw.event({"t": "m", "x": -5, "y": 9})
    raw.event({"t": "c", "c": "ş"})
    raw.event({"t": "w", "d": -8})
    raw.event({"t": "k", "vk": 0x25, "d": 1})
    until(lambda: len(sent) == 4, 5, "enjeksiyon")
    assert sent[0] == [("m", 0, 65535, 0, 0x8001)]  # sıkıştırılmış mutlak hareket
    assert sent[1] == [("k", 0, 0x15F, 4), ("k", 0, 0x15F, 6)]  # Unicode aşağı+yukarı
    assert sent[2] == [("m", 0, 0, 0xFFFFFFF8, 0x0800)]  # küçük negatif tekerlek, ikiye tümleyen
    assert sent[3][0][1] == 0x25 and sent[3][0][3] == 1  # genişletilmiş (ok) tuşu, aşağı
    raw.close()
    until(lambda: len(sent) == 5, 5, "release_all")
    assert sent[4] == [("k", 0x25, sent[3][0][2], 3)]  # oturum bitince takılı ok tuşu bırakıldı


def test_release_all_runs_once_at_session_end_after_the_last_event(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    c.send_input({"t": "k", "vk": 0x10, "d": 1})
    until(lambda: event_calls(r), 5, "olay")
    assert ("release_all",) not in r.inj.calls  # oturum sürerken çağrılmaz
    c.stop()
    r.sessions_over(1)
    assert r.inj.calls == [("key", 0x10, True), ("release_all",)]


def test_client_q_ends_session_cleanly(rig):
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    raw.ch.send(b"Q")
    r.sessions_over(1)
    assert r.inj.calls == [("release_all",)]


def test_idle_timeout_ends_silent_session(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "IDLE_TIMEOUT", 0.6)
    r = rig()
    r.start()
    raw = r.raw()
    raw.frame()
    t0 = time.monotonic()
    r.sessions_over(1)  # istemci sustu (soket açık): sessizlik sınırında oturum biter, takılı tuş bırakılır
    assert 0.5 <= time.monotonic() - t0 < 4


def test_client_coalesces_consecutive_moves_and_keeps_other_events_in_order():
    ui = ClientUI()
    c = clientmod.Client("127.0.0.1:1", "x", ui, UID)
    c.send_input({"t": "m", "x": 0, "y": 0})
    assert not c._pending  # oturum yokken girdi bayat tık olarak birikmez
    c._live = True
    for i in range(50):
        c.send_input({"t": "m", "x": i / 100, "y": 0})
    c.send_input({"t": "b", "b": "l", "d": 1, "x": 0.5, "y": 0.5})
    c.send_input({"t": "m", "x": 0.1, "y": 0.1})
    c.send_input({"t": "m", "x": 0.2, "y": 0.2})
    c.send_input({"t": "c", "c": "ş"})
    c.send_input({"t": "k", "vk": 13, "d": 1})
    got = [json.loads(d) for _, d in c._pending]
    assert got == [{"t": "m", "x": 0.49, "y": 0}, {"t": "b", "b": "l", "d": 1, "x": 0.5, "y": 0.5},
                   {"t": "m", "x": 0.2, "y": 0.2}, {"t": "c", "c": "ş"}, {"t": "k", "vk": 13, "d": 1}]
    assert all(d.isascii() for _, d in c._pending)  # \u kaçışı: vekil karakter bile kodlamayı patlatmaz
    c.send_input({"t": "c", "c": "\ud83d"})
    c.send_input({1, 2})  # JSON'a çevrilemeyen: sessizce atılır
    assert len(c._pending) == 6


# --- onay, güven, kick --------------------------------------------------------------


def test_consent_once_per_uid_and_remembered(rig):
    r = rig()
    r.start()
    c1, cu1 = r.connected_client(uid=UID)
    assert len(r.ui.of("consent")) == 1
    assert r.cfg["trusted"] == [sha(UID)] and r.saved[-1]["trusted"] == [sha(UID)]
    c1.stop()
    r.sessions_over(1)
    c2, cu2 = r.connected_client(uid=UID)  # aynı uid: sessiz kabul
    assert len(r.ui.of("consent")) == 1
    c2.stop()
    r.sessions_over(2)
    c3, cu3 = r.connected_client(uid=UID2)  # yeni uid: yeniden sorulur
    assert len(r.ui.of("consent")) == 2 and r.cfg["trusted"] == [sha(UID), sha(UID2)]
    assert [a[0] for a in r.ui.of("consent")] == [socket.gethostname()[:64]] * 2


def test_deny_is_fatal_denied_and_nothing_is_remembered(rig):
    r = rig()
    r.ui.consent_answer = False
    r.start()
    c, cu = r.client()
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("denied", "Host bağlantıya izin vermedi", True)
    assert len(cu.closings) == 1 and not cu.frames  # yeniden deneme yok
    assert r.ui.of("connected") == [] and r.cfg["trusted"] == [] and r.saved == []
    r.ui.consent_answer = True  # reddetmek hiçbir şeyi kalıcılaştırmadı
    c2, cu2 = r.connected_client()
    assert len(r.ui.of("consent")) == 2


def test_consent_timeout_reports_timeout_and_a_late_allow_is_ignored(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "CONSENT_TIMEOUT", 0.8)
    r = rig()
    gate = r.gate()
    r.ui.consent_answer = lambda n, a: gate.wait(10) and True
    r.start()
    c, cu = r.client()
    t0 = time.monotonic()
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("denied", "Host bağlantıya izin vermedi (zaman aşımı)", True)
    assert 0.7 <= time.monotonic() - t0 < 4
    gate.set()  # pencereye geç "izin ver" denmesi artık bir şeyi değiştirmez
    time.sleep(0.3)
    assert r.cfg["trusted"] == [] and r.ui.of("connected") == []


def test_consent_window_closing_at_its_own_deadline_counts_as_timeout(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "CONSENT_TIMEOUT", 1.0)
    r = rig()
    r.ui.consent_answer = lambda n, a: time.sleep(1.0) or False  # arayüzün kendi geri sayımı bitince False döner
    r.start()
    c, cu = r.client()
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[2].endswith("(zaman aşımı)")


def test_stop_denies_a_pending_consent_at_once(rig):
    r = rig()
    gate = r.gate()
    r.ui.consent_answer = lambda n, a: gate.wait(10) and True
    r.start()
    c, cu = r.client()
    until(lambda: r.ui.of("consent"), 8, "onay penceresi")
    t0 = time.monotonic()
    r.host.stop()
    assert time.monotonic() - t0 < 0.25
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1] in ("kicked", "denied")  # Q ya da D: ikisi de kesin
    assert time.monotonic() - t0 < 4
    assert r.ui.of("connected") == [] and r.cfg["trusted"] == []


def test_second_untrusted_attempt_during_consent_gets_busy(rig):
    r = rig()
    gate = r.gate()
    r.ui.consent_answer = lambda n, a: gate.wait(10) and True
    r.start()
    first = r.client(uid=UID)
    until(lambda: r.ui.of("consent"), 8, "onay penceresi")
    # oturum yuvası onay beklenmeden adım 5'te alınır: ikinci deneme hello okunmadan "meşgul" reddi görür
    with pytest.raises(proto.AuthError) as e:
        r.raw(uid=UID2, ready=False)
    assert e.value.reason == "B"  # tek onay penceresi: ikincisi meşgul
    assert len(r.ui.of("consent")) == 1


@pytest.mark.parametrize("hello", [
    b"json degil", b'{"uid":"a1"}', b'{"name":"x"}', b'["a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"]',
    b'{"uid":"A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1"}', b'{"uid":5}', b'{"uid":"' + b"a1" * 17 + b'"}',
])
def test_invalid_hello_is_rejected_without_asking_the_owner(rig, hello):
    r = rig()
    r.start()
    raw = r.raw(ready=False)  # geçerli ilk H: bir kez onay ister ve kabul olur (tek oturum yuvasını işgal eder)
    raw.ch.send(b"X")  # ilk gönderilen H, bu ikinci: önce geçersiz H'yi yeniden kur
    raw.close()
    r.sessions_over(1)  # yuvanın boşalmasını bekle: yoksa "bad" meşgul reddiyle yarışa girer
    before = len(r.ui.of("consent"))
    bad = Raw.__new__(Raw)
    bad.sock = socket.create_connection(tuple_addr(r.host.conn_id), 5)
    bad.ch = proto.client_handshake(bad.sock, r.host.conn_id, KEY)
    bad.sock.settimeout(5)
    r.raws.append(bad)
    bad.ch.send(b"H", hello)
    with pytest.raises(proto.AuthError) as e:
        bad.ch.recv()
    assert e.value.reason == "I" and len(r.ui.of("consent")) == before  # geçersiz hello yeniden onay istemez


def tuple_addr(conn_id):
    h, _, port = conn_id.rpartition(":")
    return h, int(port)


def test_first_frame_of_wrong_kind_is_rejected(rig):
    r = rig()
    r.start()
    bad = Raw.__new__(Raw)
    bad.sock = socket.create_connection(tuple_addr(r.host.conn_id), 5)
    bad.ch = proto.client_handshake(bad.sock, r.host.conn_id, KEY)
    bad.sock.settimeout(5)
    r.raws.append(bad)
    bad.ch.send(b"I", b"{}")
    with pytest.raises(proto.AuthError) as e:
        bad.ch.recv()
    assert e.value.reason == "I" and r.ui.of("consent") == []


def test_peer_name_is_sanitized_before_it_reaches_the_owner(rig):
    r = rig()
    r.start()
    raw = r.raw(name="‮" + "a" * 100 + "\n\x00")  # bidi geçersiz kılma + satır sonu + kontrol karakteri + uzunluk
    raw.frame()
    assert r.ui.of("consent")[0][0] == "a" * 64 and r.ui.of("connected")[0][0] == "a" * 64


def test_kick_is_fatal_for_client_removes_trust_and_next_connect_asks_again(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    assert r.cfg["trusted"] == [sha(UID)]
    t0 = time.monotonic()
    r.host.kick()
    assert time.monotonic() - t0 < 0.25
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("kicked", "Host bağlantıyı kesti", True) and len(cu.closings) == 1  # yeniden deneme yok
    r.sessions_over(1)
    assert r.cfg["trusted"] == [] and r.saved[-1]["trusted"] == []
    c2, cu2 = r.connected_client()  # güven kalktı: yeniden onay istenir
    assert len(r.ui.of("consent")) == 2
    assert r.inj.calls[-1] == ("release_all",) or ("release_all",) in r.inj.calls


def test_kick_without_session_does_nothing(rig):
    r = rig()
    r.start()
    r.host.kick()
    time.sleep(0.2)
    assert r.saved == [] and r.ui.of("disconnected") == []


def test_stop_with_active_session_tells_client_and_cleans_up(rig, sandbox):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    t0 = time.monotonic()
    r.host.stop()
    assert time.monotonic() - t0 < 0.25
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1] == "kicked" and len(cu.closings) == 1
    r.sessions_over(1)
    assert r.inj.calls[-1] == ("release_all",)
    r.host.stop()  # ikinci stop zararsız
    until(lambda: sandbox.ngrok.names().count("kill") >= 2, 5, "ngrok.kill")


# --- parola, kimlik, bağlantı sınırları -------------------------------------------


def test_wrong_password_is_fatal_auth_after_the_tarpit_and_host_keeps_serving(rig):
    r = rig()
    r.secrets.append(WRONG)
    r.start()
    t0 = time.monotonic()
    c, cu = r.client(password=WRONG)
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("auth", "Şifre yanlış", True) and len(cu.closings) == 1
    assert time.monotonic() - t0 >= 0.05  # fail_delay
    assert not cu.frames and r.ui.of("consent") == []
    c2, cu2 = r.connected_client()  # Host başkalarına hizmet vermeye devam eder
    assert cu2.frames
    assert any("Yanlış şifre" in t for t in r.ui.texts())


def test_wrong_password_attempts_hold_only_preauth_slots_not_the_session(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    for _ in range(3):
        bad = r.raw(key=proto.derive_key(WRONG), ready=False) if False else None
    # etkin oturum varken yanlış şifreli deneme: meşgul değil, önce "meşgul" bakışı çalışır
    raw = Raw.__new__(Raw)
    raw.sock = socket.create_connection(tuple_addr(r.host.conn_id), 5)
    r.raws.append(raw)
    raw.sock.settimeout(5)
    raw.sock.sendall(b"UM1" + bytes(49))
    assert raw.sock.recv(5) == proto.REJECT + b"B"  # etkin oturum: hello okunmadan meşgul
    assert cu.frames  # asıl oturum etkilenmedi


def test_wrong_id_is_fatal_id(rig):
    r = rig()
    r.start()
    r.host.conn_id = "baska.host:1"  # Host başka bir ID bekliyor
    c, cu = r.client(conn_id="127.0.0.1:" + str(tuple_addr(r.host._listener.getsockname()[0] + ":" + str(r.host._listener.getsockname()[1]))[1]))
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("id", "ID yanlış", True) and len(cu.closings) == 1


def test_ninth_simultaneous_preauth_connection_is_refused(rig):
    r = rig()
    r.start()
    addr = tuple_addr(r.host.conn_id)
    held = []
    try:
        for _ in range(8):  # hello yollamayan sekiz bağlantı ön-doğrulama yuvalarını tutar
            held.append(socket.create_connection(addr, 5))
            time.sleep(0.05)
        ninth = socket.create_connection(addr, 5)
        held.append(ninth)
        ninth.settimeout(1.5)
        try:
            assert ninth.recv(1) == b""  # 9.: hemen kapatılır
        except ConnectionError:
            pass
        for s in held[:8]:  # ilk sekiz hâlâ açık (ne veri ne EOF)
            s.settimeout(0.3)
            with pytest.raises(socket.timeout):
                s.recv(1)
    finally:
        for s in held:
            s.close()
    c, cu = r.connected_client()  # yuvalar boşalınca normal istemci bağlanır
    assert cu.frames


def test_second_client_while_busy_gets_busy_then_succeeds_after_first_ends(rig):
    r = rig()
    r.start()
    c1, cu1 = r.connected_client(uid=UID)
    c2, cu2 = r.client(uid=UID2)
    until(lambda: cu2.closings, 8, "meşgul bildirimi")
    first = cu2.closings[0]
    assert first[1:] == ("busy", "Host meşgul, tekrar deneniyor… (1/20)", False)  # ilk denemede de yeniden denenir
    assert len(r.ui.of("consent")) == 1 and not cu2.frames  # meşgulken onay penceresi açılmaz
    c1.stop()
    until(lambda: cu2.frames, 8, "ikinci istemcinin kareleri")
    assert cu2.final is None
    assert len(r.ui.of("consent")) == 2


# --- kopma ve yeniden bağlanma ----------------------------------------------------


def test_half_open_drop_reconnects_after_three_seconds_and_frames_resume(rig, sandbox, monkeypatch):
    sandbox.ngrok.use_proxy = True
    monkeypatch.setattr(clientmod, "RETRY_DELAY", 3.0)  # SPEC değeri
    for mod in (hostmod, clientmod):
        monkeypatch.setattr(mod, "IDLE_TIMEOUT", 1.0)
        monkeypatch.setattr(mod, "KEEPALIVE", 0.3)
    r = rig()
    r.start()
    c, cu = r.connected_client()
    sandbox.ngrok.proxies[0].blackhole()  # iki yön de sessizce yutulur, soketler açık kalır
    until(lambda: cu.closings, 8, "kopma bildirimi")
    t_lost, code, text, fatal = cu.closings[0]
    assert (code, fatal) == ("lost", False) and text == "Bağlantı koptu, tekrar deneniyor… (1/20)"
    until(lambda: any(t > t_lost and s == "Bağlanıyor…" for t, s in list(cu.statuses)), 8, "yeniden deneme")
    t_retry = next(t for t, s in cu.statuses if t > t_lost and s == "Bağlanıyor…")
    assert 2.7 <= t_retry - t_lost <= 4.5  # ilk yeniden deneme ~3 sn sonra
    r.screen.change()
    until(lambda: cu.frames[-1] == 101, 8, "kareler yeniden akıyor")
    assert cu.final is None and len(r.ui.of("consent")) == 1  # güvenilen uid: sessiz yeniden bağlanma
    assert len(r.ui.of("connected")) == 2


def test_host_vanishing_without_q_reconnects_after_session_slot_frees(rig):
    r = rig()
    r.start()
    c, cu = r.connected_client()
    r.host._ch.close()  # Host tarafı Q yollamadan düştü
    until(lambda: cu.closings, 8, "kopma bildirimi")
    assert cu.closings[0][1] == "lost" and cu.closings[0][3] is False
    r.screen.change()
    until(lambda: cu.frames[-1] == 101, 8, "yeniden bağlanma")


# --- istemci tek başına: sahte Host ile -----------------------------------------------


class FakeHost:
    """Elle yazılmış tek bağlantılık Host: gerçek el sıkışma + H okuma, sonra betik. Client'ın her çerçevesi received'a düşer."""

    def __init__(self, script):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.srv.settimeout(10)
        self.conn_id = "127.0.0.1:%d" % self.srv.getsockname()[1]
        self.script, self.received, self.release = script, [], threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn = None
        try:
            conn, _ = self.srv.accept()
            ch = proto.server_handshake(conn, self.conn_id, KEY)
            kind, self.hello = ch.recv()
            ch.trust()
            threading.Thread(target=self._read, args=(ch,), daemon=True).start()
            self.script(ch, self)
        except (OSError, proto.AuthError):
            pass
        finally:
            if conn is not None:
                conn.close()

    def _read(self, ch):
        try:
            while True:
                self.received.append(ch.recv())
        except (OSError, proto.AuthError):
            pass

    def close(self):
        self.release.set()
        self.srv.close()


@pytest.fixture
def fake_host():
    made = []

    def make(script):
        made.append(FakeHost(script))
        return made[-1]

    yield make
    for fh in made:
        fh.close()


def jpeg(w=64, h=48):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG", optimize=True)
    return buf.getvalue()


def run_client(conn_id, ui=None, **kw):
    ui = ui or ClientUI()
    c = clientmod.Client(conn_id, PASSWORD, ui, UID, **kw)
    c.start()
    return c, ui


def test_client_handles_p_f_q_corrupt_and_hostile_frames(fake_host, monkeypatch):
    monkeypatch.setattr(clientmod, "KEEPALIVE", 0.2)
    huge = bytearray(jpeg(16, 16))
    i = huge.index(b"\xff\xc0")
    huge[i + 5:i + 9] = (20000).to_bytes(2, "big") * 2  # başlık 20000x20000 (400 MP) diyor: çözmeden atılmalı

    def script(ch, fh):
        ch.send(b"W", b'{"name":"x","v":1}')
        ch.send(b"P", b"L")  # kilitli
        time.sleep(0.15)
        ch.send(b"F", jpeg(70, 40))  # kare geliyorsa kilit kalkmıştır: durum "Bağlandı"na döner
        ch.send(b"F", b"JPEG degil")  # bozuk kare: atlanır, yine de ack
        ch.send(b"F", bytes(huge))  # dev boyutlu başlık: atlanır, yine de ack
        ch.send(b"Z", b"bilinmeyen")  # bilinmeyen tür yok sayılır
        ch.send(b"P", b"L")
        time.sleep(0.15)
        ch.send(b"P", b"")  # kilit kalktı
        time.sleep(0.7)  # istemci kendi canlılık P'sini yollasın
        ch.send(b"Q")
        fh.release.wait(5)

    fh = fake_host(script)
    c, ui = run_client(fh.conn_id)
    until(lambda: ui.final, 8, "kapanış")
    assert ui.final[1:] == ("kicked", "Host bağlantıyı kesti", True)
    assert ui.frames == [70]  # yalnız geçerli kare gösterildi
    assert ui.texts() == ["Bağlanıyor…", "Host onayı bekleniyor…", "Bağlandı", "Host ekranı kilitli / UAC açık",
                          "Bağlandı", "Host ekranı kilitli / UAC açık", "Bağlandı"]
    kinds = [k for k, _ in fh.received]
    assert kinds.count(b"A") == 3  # üç F'nin üçüne de ack
    assert b"P" in kinds  # sessizlikte canlılık
    assert json.loads(fh.hello) == {"uid": UID, "name": socket.gethostname()[:64], "v": 1}


def test_client_sends_input_as_compact_ascii_json_in_order(fake_host):
    def script(ch, fh):
        ch.send(b"W", b"{}")
        fh.release.wait(8)

    fh = fake_host(script)
    c, ui = run_client(fh.conn_id)
    until(lambda: "Bağlandı" in ui.texts(), 8, "bağlantı")
    for ev in ({"t": "c", "c": "ş"}, {"t": "k", "vk": 13, "d": 1}, {"t": "w", "d": -8}):
        c.send_input(ev)
    until(lambda: [p for k, p in fh.received if k == b"I"].__len__() == 3, 5, "olaylar")
    assert [p for k, p in fh.received if k == b"I"] == [
        b'{"t":"c","c":"\\u015f"}', b'{"t":"k","vk":13,"d":1}', b'{"t":"w","d":-8}']
    # test burada bırakırsa istemci iş parçacığı fake_host kapandıktan sonra "unreachable" ile 20 kez
    # yeniden bağlanmayı deneyip ~6 sn arkada kalır: sonraki testlerin "client-" iş parçacığı beklemesini yer
    c.stop()
    until(lambda: ui.final, 5, "kapanış")
    c._thread.join(5)


@pytest.mark.parametrize("conn_id, relay_arg, fragment", [
    ("abc", None, "ID anlaşılmadı"), ("sunucu:port", None, "sunucu:port"), ("sunucu:0", None, "sunucu:port"),
    ("sunucu:99999", None, "sunucu:port"), (":5", None, "sunucu:port"), ("١٢٣", None, "ID anlaşılmadı"),
    ("", None, "ID anlaşılmadı"), ("123456789", None, "relay adresi"), ("123 456 789", None, "relay adresi"),
    ("123456789", ("h", 1, "tok\nen"), "tek satır"),
])
def test_bad_id_input_is_fatal_input_without_touching_the_network(monkeypatch, conn_id, relay_arg, fragment):
    monkeypatch.setattr(socket, "create_connection", tripwire("create_connection"))
    monkeypatch.setattr(proto, "derive_key", tripwire("derive_key"))  # anlaşılmayan ID için scrypt de çalışmaz
    c, ui = run_client(conn_id, relay=relay_arg)
    until(lambda: ui.final, 5, "kapanış")
    assert ui.final[1] == "input" and fragment in ui.final[2] and len(ui.closings) == 1 and ui.statuses == []


def test_unreachable_first_attempt_is_fatal_not_retried():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # kimse dinlemiyor
    c, ui = run_client("127.0.0.1:%d" % port)
    until(lambda: ui.final, 10, "kapanış")  # Windows'ta 'reddedildi' ~2 sn sürer
    assert ui.final[1] == "unreachable" and ui.final[2].startswith("Host'a ulaşılamadı")
    assert len(ui.closings) == 1


def test_retry_budget_is_twenty_by_default_and_exhaustion_is_fatal(monkeypatch):
    assert clientmod.RETRIES == 20 and hostmod.IDLE_TIMEOUT == clientmod.IDLE_TIMEOUT == 10
    monkeypatch.setattr(clientmod, "RETRIES", 3)
    monkeypatch.setattr(clientmod, "RETRY_DELAY", 0.05)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.3)
    stop = threading.Event()

    def closer():  # her bağlantıyı hello'yu okumadan kapatır: el sıkışmada kopma = "lost"
        while not stop.is_set():
            try:
                srv.accept()[0].close()
            except OSError:
                continue

    threading.Thread(target=closer, daemon=True).start()
    try:
        c, ui = run_client("127.0.0.1:%d" % srv.getsockname()[1])
        until(lambda: ui.final, 10, "kapanış")
    finally:
        stop.set()
        srv.close()
    assert [(code, fatal) for _, code, _, fatal in ui.closings] == [("lost", False)] * 3 + [("lost", True)]
    assert [t for _, _, t, _ in ui.closings[:3]] == ["Bağlantı koptu, tekrar deneniyor… (%d/3)" % i for i in (1, 2, 3)]
    assert ui.final[2] == "Bağlantı koptu (deneme hakkı bitti)"


def test_stop_ends_client_quickly_while_waiting_for_host_answer(rig):
    r = rig()
    gate = r.gate()
    r.ui.consent_answer = lambda n, a: gate.wait(10) and True
    r.start()
    c, cu = r.client()
    until(lambda: "Host onayı bekleniyor…" in cu.texts(), 8, "onay bekleme")
    t0 = time.monotonic()
    c.stop()
    assert time.monotonic() - t0 < 0.25
    until(lambda: cu.final, 5, "kapanış")
    assert cu.final[1] == "stopped" and len(cu.closings) == 1
    c._thread.join(5)
    assert not c._thread.is_alive()


def test_stop_during_retry_wait_ends_with_stopped(fake_host, monkeypatch):
    monkeypatch.setattr(clientmod, "RETRY_DELAY", 30)
    fh = fake_host(lambda ch, fh_: ch.send(b"Q") if False else (ch.send(b"W", b"{}"), time.sleep(0.2)))
    c, ui = run_client(fh.conn_id)
    until(lambda: ui.closings, 8, "ilk kopma")  # betik bitince Host kapanır: "lost", 30 sn bekleme
    assert ui.closings[0][1:] == ("lost", "Bağlantı koptu, tekrar deneniyor… (1/20)", False)
    c.stop()
    until(lambda: ui.final, 5, "kapanış")
    assert ui.final[1] == "stopped"


# --- ngrok kipi -----------------------------------------------------------------------


def test_ngrok_is_prepared_with_private_paths_explicit_ipv4_and_killed_first(rig, sandbox):
    r = rig()
    conn_id = r.start()
    assert sandbox.ngrok.names() == ["kill", "connect"]  # önce yetim ngrok.exe'yi öldür
    (_, kw) = sandbox.ngrok.calls[1]
    assert set(kw) == {"addr", "proto", "pyngrok_config"} and kw["proto"] == "tcp"
    assert kw["addr"] == "tcp://127.0.0.1:%d" % int(conn_id.rpartition(":")[2])
    pc = kw["pyngrok_config"]
    assert sandbox.ngrok.calls[0][1] is pc
    assert pc.ngrok_path == os.path.join(config.APPDIR, "ngrok.exe") and pc.config_path == os.path.join(config.APPDIR, "ngrok.yml")
    assert pc.auth_token == NGROK_TOKEN and pc.startup_timeout == 30
    assert r.ui.of("ready") == [(conn_id,)] and "://" not in conn_id
    assert r.ui.of("log")[0] == ("ngrok hazırlanıyor (ilk çalıştırmada indirilir)…",)


def test_ngrok_address_form_is_translated_to_a_valid_upstream_by_real_pyngrok(rig, sandbox, tmp_path):
    # pyngrok 9: "127.0.0.1:port" biçimi "localhost:127.0.0.1:port" olur; "tcp://127.0.0.1:port" olduğu gibi geçer
    interpolate = getattr(real_ngrok, "_interpolate_tunnel_definition", None)
    if interpolate is None:
        pytest.skip("pyngrok sürümünde _interpolate_tunnel_definition yok")
    r = rig()
    r.start()
    (_, kw) = sandbox.ngrok.calls[1]
    opts = {}
    interpolate(kw["pyngrok_config"], opts, kw["addr"], kw["proto"], None)  # ağa çıkmaz: yalnız yerel ayar dosyasına bakar
    assert opts["upstream"] == {"url": kw["addr"]} and opts["url"] == "tcp://"


@pytest.mark.parametrize("cfg_token, baked, env, want", [
    ("kendi", "gömülü", "ortam", "kendi"), ("", "gömülü", "ortam", "gömülü"), ("", "", "ortam", "ortam"), ("", "", None, None),
])
def test_ngrok_token_precedence(rig, sandbox, monkeypatch, cfg_token, baked, env, want):
    monkeypatch.setattr(config, "DEFAULT_NGROK_TOKEN", baked)
    if env is None:
        monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    else:
        monkeypatch.setenv("NGROK_AUTHTOKEN", env)
    r = rig(ngrok_token=cfg_token)
    r.secrets = [s for s in r.secrets if s != NGROK_TOKEN]
    r.start()
    assert sandbox.ngrok.calls[1][1]["pyngrok_config"].auth_token == want


def test_keep_awake_is_set_and_cleared_from_the_same_long_lived_thread(rig, sandbox):
    r = rig()
    r.start()
    assert [on for on, _ in sandbox.awake] == [True]
    r.host.stop()
    until(lambda: [on for on, _ in sandbox.awake] == [True, False], 5, "uyku engelinin kalkması")
    assert len({t for _, t in sandbox.awake}) == 1 and MAIN not in {t for _, t in sandbox.awake}
    assert any("Uyku engellendi" in t for t in r.ui.texts())


def test_stop_returns_immediately_even_when_ngrok_kill_blocks(rig, sandbox):
    r = rig()
    sandbox.ngrok.kill_gate = gate = r.gate()  # stop'un kill'i bloklanır
    r.start()
    t0 = time.perf_counter()
    r.host.stop()
    assert time.perf_counter() - t0 < 0.25
    until(lambda: sandbox.ngrok.names().count("kill") == 2, 5, "stop'un ngrok.kill çağrısı")
    gate.set()


def test_stop_during_connect_tears_down_the_late_tunnel(rig, sandbox):
    r = rig()
    sandbox.ngrok.connect_gate = gate = r.gate()  # connect saniyelerce sürüyor
    r.host.start()
    until(lambda: "connect" in sandbox.ngrok.names(), 5, "connect çağrısı")
    t0 = time.perf_counter()
    r.host.stop()
    assert time.perf_counter() - t0 < 0.25
    gate.set()  # tünel şimdi geliyor: kurulup kullanıma açılmamalı, kapatılmalı
    until(lambda: sandbox.ngrok.names()[-1] == "kill" and sandbox.ngrok.names().count("kill") >= 2, 5, "geç tünelin kapatılması")
    until(lambda: not any(t.name == "host-main" for t in threading.enumerate()), 5, "ana iş parçacığı")
    assert r.ui.of("ready") == [] and r.ui.of("error") == [] and sandbox.awake == []


@pytest.mark.parametrize("message, want", [
    ("authentication failed: The authtoken you specified does not look like a proper ngrok authtoken. ERR_NGROK_105", "Authtoken"),
    ("authentication failed: Usage of ngrok requires a verified account and authtoken. ERR_NGROK_4018", "Authtoken"),
    ("failed to start tunnel: You must add a credit or debit card before you can use TCP endpoints. ERR_NGROK_8013", "kart doğrulaması"),
    ("authentication failed: Your account is limited to 1 simultaneous ngrok agent sessions. ERR_NGROK_108", "ngrok.exe'yi kapatın"),
    ("An error occurred while downloading ngrok from https://x: <urlopen error [Errno 11001] getaddrinfo failed>", "İnternet"),
    ("tamamen tanınmayan bir şey", "ngrok başlatılamadı"),
])
def test_ngrok_errors_give_a_turkish_hint_and_clean_up(rig, sandbox, message, want):
    r = rig()
    sandbox.ngrok.fail = RuntimeError(message + " token=" + NGROK_TOKEN)
    r.host.start()
    until(lambda: r.ui.of("error"), 5, "hata")
    ((text,),) = r.ui.of("error")
    assert want in text and message[:30] in text  # ipucu + ham ayrıntı
    assert NGROK_TOKEN not in text and "***" in text  # token asla görünmez
    until(lambda: sandbox.ngrok.names() == ["kill", "connect", "kill"], 5, "yarım kalan ngrok'un temizlenmesi")
    assert r.ui.of("ready") == [] and sandbox.awake == []
    until(lambda: not any(t.name == "host-main" for t in threading.enumerate()), 5, "ana iş parçacığı")


def test_error_after_stop_is_not_reported(rig, sandbox):
    r = rig()
    sandbox.ngrok.connect_gate = gate = r.gate()
    sandbox.ngrok.fail = RuntimeError("iptal edildi")
    r.host.start()
    until(lambda: "connect" in sandbox.ngrok.names(), 5, "connect")
    r.host.stop()
    gate.set()
    until(lambda: not any(t.name == "host-main" for t in threading.enumerate()), 5, "ana iş parçacığı")
    assert r.ui.of("error") == []


def test_importing_the_engines_never_touches_network_or_processes():
    code = (
        "import socket, subprocess, sys\n"
        "def boom(*a, **k): raise AssertionError('import sırasında ağ/süreç kullanımı')\n"
        "socket.socket.connect = boom; socket.create_connection = boom; subprocess.Popen = boom\n"
        "sys.path.insert(0, %r)\n"
        "import uzakmasa.host, uzakmasa.client\n"
        "print('ok')\n" % str(ROOT)
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_constructor_validates_mode_and_key(sandbox):
    base = {"k": KEY.hex(), "trusted": []}
    with pytest.raises(ValueError):
        hostmod.Host(base, HostUI(), "tünel")
    for bad in ("", "zz", "ab" * 31, "ab" * 33):
        with pytest.raises(ValueError):
            hostmod.Host({"k": bad}, HostUI(), "ngrok")


# --- genel: istatistik, kilit, sır -------------------------------------------------


def test_stats_callback_fires_and_counts_across_sessions(rig, monkeypatch):
    monkeypatch.setattr(hostmod, "STATS_EVERY", 0.2)
    r = rig()
    r.screen.auto = True
    r.start()
    c, cu = r.connected_client()
    until(lambda: len(r.ui.of("stats")) >= 3, 5, "istatistikler")
    c.stop()
    r.sessions_over(1)
    first = [a[0] for a in r.ui.of("stats")]
    assert first == sorted(first) and first[-1] > 0 and all(type(v) is int for v in first)
    n = len(first)
    c2, cu2 = r.connected_client()
    until(lambda: len(r.ui.of("stats")) > n, 5, "ikinci oturum istatistiği")
    assert r.ui.of("stats")[n][0] >= first[-1]  # sayaç oturumlar arasında birikir (ngrok aylık kotası için)


def test_locked_screen_keepalive_carries_l_and_pauses_grabbing(rig, sandbox, monkeypatch):
    monkeypatch.setattr(hostmod, "KEEPALIVE", 0.3)
    sandbox.locked = True
    r = rig()
    r.start()
    raw = r.raw()  # kilitliyken de oturum kurulur
    got = raw.drain(1.3)
    assert len(got) >= 2 and all(k == b"P" and p == b"L" for k, p in got)  # kare yok, P'ler kilidi bildirir
    assert r.screen.calls == 0  # kilitliyken yakalama denenmez
    sandbox.locked = False
    assert raw.frame() == 100
    assert all(p == b"" for k, p in raw.drain(0.8) if k == b"P")
    assert sum(1 for t in r.ui.texts() if "kilit" in t) == 2  # durum değişince bir kez: kilitlendi, açıldı


def test_client_shows_locked_status_and_restores_it(rig, sandbox, monkeypatch):
    monkeypatch.setattr(hostmod, "KEEPALIVE", 0.2)
    sandbox.locked = True
    r = rig()
    r.start()
    c, cu = r.client()
    until(lambda: "Host ekranı kilitli / UAC açık" in cu.texts(), 8, "kilit durumu")
    sandbox.locked = False
    until(lambda: cu.texts()[-1] == "Bağlandı" and cu.frames, 8, "durumun geri gelmesi")


def test_no_secrets_in_logs_after_busy_paths(rig):
    r = rig()
    r.secrets.append(WRONG)
    r.start()
    c, cu = r.connected_client()
    for _ in range(2):
        # etkin oturum varken yeni deneme hello bile okunmadan meşgul reddi alır (adım 1): şifre hiç denenmez
        with pytest.raises(proto.AuthError) as exc:
            Raw(r.host.conn_id, key=proto.derive_key(WRONG), ready=False)
        assert exc.value.reason == "B"
    c.send_input({"t": "k", "vk": 999, "d": 1})  # günlüğe yazılan olay içeriği değil, yalnız sebep olmalı
    until(lambda: any("Girdi olayı" in t for t in r.ui.texts()), 5, "girdi uyarısı")
    r.host.kick()
    r.sessions_over(1)
    assert r.ui.texts()  # kontrol anlamlı olsun: log'lar gerçekten var
    assert not any("999" in t for t in r.ui.texts())


# --- relay üzerinden ------------------------------------------------------------------


def relay_rig(rig, rt, **kw):
    r = rig("relay", relay="127.0.0.1:%d" % rt.port, relay_token=RELAY_TOKEN, **kw)
    r.secrets.append(RELAY_TOKEN)
    return r


def relay_client(r, rt, **kw):
    kw.setdefault("relay", ("127.0.0.1", rt.port, RELAY_TOKEN))
    return r.client(conn_id="123 456 789", **kw)


def registered(rt, hid=RELAY_ID):
    return hid in rt.relay.waiting


def test_relay_full_session_input_reregistration_and_kick(rig, relays):
    rt = relays()
    r = relay_rig(rig, rt)
    assert r.start() == RELAY_ID and r.host.mode == "relay"
    until(lambda: registered(rt), 5, "relay kaydı")
    c, cu = relay_client(r, rt)
    until(lambda: cu.frames, 8, "ilk kare")
    assert cu.texts()[:3] == ["Relay'e bağlanılıyor…", "Host onayı bekleniyor…", "Bağlandı"]
    r.screen.change()
    until(lambda: cu.frames[-1] == 101, 5, "ikinci kare")
    c.send_input({"t": "c", "c": "ş"})
    until(lambda: event_calls(r), 5, "girdi")
    assert event_calls(r) == [("char", "ş")]
    c.stop()
    r.sessions_over(1)
    until(lambda: registered(rt), 5, "oturumdan sonra yeniden kayıt")  # Host kendini yeniden kaydeder
    c2, cu2 = relay_client(r, rt)  # güvenilen uid: onaysız
    until(lambda: cu2.frames, 8, "ikinci oturum")
    assert len(r.ui.of("consent")) == 1
    r.host.kick()
    until(lambda: cu2.final, 8, "kick")
    assert cu2.final[1] == "kicked" and r.cfg["trusted"] == []
    assert len(r.ui.of("consent")) == 1


def test_relay_token_is_enforced_for_client_and_host(rig, relays):
    rt = relays()
    r = relay_rig(rig, rt)
    r.start()
    until(lambda: registered(rt), 5, "relay kaydı")
    c, cu = relay_client(r, rt, relay=("127.0.0.1", rt.port, "yanlış token"))
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1:] == ("input", "Relay token'ı yanlış", True) and len(cu.closings) == 1
    assert registered(rt)  # yanlış tokenlı istemci bekleyen Host'u düşüremez
    c2, cu2 = relay_client(r, rt)
    until(lambda: cu2.frames, 8, "doğru token")
    bad = rig("relay", relay="127.0.0.1:%d" % rt.port, relay_token="yanlış", relay_id="987654321")
    bad.host.start()
    until(lambda: bad.ui.of("error"), 5, "Host hatası")
    assert "token" in bad.ui.of("error")[0][0]
    time.sleep(0.4)
    assert len(bad.ui.of("error")) == 1 and bad.ui.of("ready") == [] and not registered(rt, "987654321")  # tekrar denemez


def test_relay_unknown_host_is_fatal_offline_on_first_attempt(rig, relays):
    rt = relays()
    r = relay_rig(rig, rt)  # Host hiç başlatılmadı
    c, cu = relay_client(r, rt)
    until(lambda: cu.final, 8, "kapanış")
    assert cu.final[1] == "offline" and len(cu.closings) == 1


def test_relay_unreachable_first_attempt_is_fatal(rig, relays):
    rt = relays()
    port = rt.port
    rt.stop()
    r = rig("relay", relay="127.0.0.1:%d" % port, relay_token=RELAY_TOKEN)
    c, cu = r.client(conn_id="123456789", relay=("127.0.0.1", port, RELAY_TOKEN))
    until(lambda: cu.final, 10, "kapanış")
    assert cu.final[1:] == ("unreachable", "Relay sunucusuna ulaşılamadı", True)


def test_relay_heartbeat_k_lines_are_ignored_by_the_host(rig, relays, monkeypatch):
    monkeypatch.setattr(relay, "HEARTBEAT", 0.2)
    rt = relays()
    r = relay_rig(rig, rt)
    r.start()
    until(lambda: registered(rt), 5, "relay kaydı")
    writer = rt.relay.waiting[RELAY_ID][1]
    time.sleep(1.0)  # birkaç K geldi
    assert rt.relay.waiting[RELAY_ID][1] is writer  # K'lar kaydı bozmadı, yeniden kayıt gerekmedi
    c, cu = relay_client(r, rt)
    until(lambda: cu.frames, 8, "oturum")
    assert len(r.ui.of("ready")) == 1


def test_host_reregisters_after_waiting_socket_timeout(rig, relays, monkeypatch):
    monkeypatch.setattr(hostmod, "RELAY_WAIT", 0.4)  # K hiç gelmiyor (HEARTBEAT 20 sn): kayıt ölü sayılır
    monkeypatch.setattr(hostmod, "RETRY_MIN", 0.05)
    rt = relays()
    r = relay_rig(rig, rt)
    r.start()
    until(lambda: registered(rt), 5, "relay kaydı")
    first = rt.relay.waiting[RELAY_ID][1]
    until(lambda: len(r.ui.of("ready")) >= 3, 6, "yeniden kayıtlar")
    until(lambda: registered(rt) and rt.relay.waiting[RELAY_ID][1] is not first, 3, "yeni kayıt")
    c, cu = relay_client(r, rt)  # yeniden kaydolan Host hâlâ bağlanılabilir
    until(lambda: cu.frames, 8, "oturum")


def test_healthy_registration_loss_reregisters_immediately_but_flapping_backs_off(rig, relays, monkeypatch):
    monkeypatch.setattr(hostmod, "RELAY_HOLD_MIN", 0.0)  # her kayıt sağlıklı sayılır
    monkeypatch.setattr(hostmod, "RELAY_WAIT", 0.3)
    rt = relays()
    r = relay_rig(rig, rt)
    r.start()
    t0 = time.monotonic()
    until(lambda: len(r.ui.of("ready")) >= 4, 6, "hemen yeniden kayıtlar")
    assert time.monotonic() - t0 < 2.0  # 2 sn'lik geri çekilme uygulanmadı


def test_relay_that_closes_right_after_ok_gets_backoff_not_a_hot_loop(rig, monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.3)
    stop, regs = threading.Event(), []

    def fake_relay():
        while not stop.is_set():
            try:
                s, _ = srv.accept()
            except OSError:
                continue
            try:
                s.settimeout(2)
                regs.append(proto.read_line(s))
                s.sendall(b"OK\n")
            except (OSError, ValueError):
                pass
            s.close()

    threading.Thread(target=fake_relay, daemon=True).start()
    try:
        r = rig("relay", relay="127.0.0.1:%d" % srv.getsockname()[1], relay_token=RELAY_TOKEN)
        r.secrets.append(RELAY_TOKEN)
        r.host.start()
        time.sleep(1.2)
        assert 1 <= len(regs) <= 2  # varsayılan geri çekilme 2 sn: saniyede onlarca kayıt yok
        assert regs[0] == "H %s %s" % (RELAY_ID, RELAY_TOKEN)
    finally:
        stop.set()
        srv.close()


def test_host_reregisters_after_relay_restart_and_client_reconnects(rig, relays, monkeypatch):
    monkeypatch.setattr(hostmod, "RETRY_MIN", 0.05)
    monkeypatch.setattr(hostmod, "RETRY_MAX", 0.3)
    rt = relays()
    port = rt.port
    r = relay_rig(rig, rt)
    r.start()
    c, cu = relay_client(r, rt)
    until(lambda: cu.frames, 8, "ilk oturum")
    rt.stop()  # relay çöktü: tüm bağlantılar kapanır
    until(lambda: cu.closings, 8, "kopma bildirimi")
    assert cu.closings[0][1] == "lost" and cu.closings[0][3] is False
    rt2 = None
    for _ in range(100):  # aynı porta geri gel (Windows'ta bırakma gecikebilir)
        try:
            rt2 = relays(port=port)
            break
        except OSError:
            time.sleep(0.05)
    assert rt2 is not None
    until(lambda: registered(rt2), 10, "Host'un yeni relay'e kaydolması")
    r.screen.change()
    until(lambda: cu.frames[-1] == 101, 15, "istemcinin yeniden bağlanıp karelerin akması")
    assert cu.final is None and len(r.ui.of("consent")) == 1
    codes = {code for _, code, _, fatal in cu.closings}
    assert codes <= {"lost", "unreachable", "offline"} and not any(f for *_, f in cu.closings)  # hepsi yeniden denenen kodlar


def test_relay_stop_is_immediate_and_unregisters_the_host(rig, relays, sandbox, monkeypatch):
    monkeypatch.setattr(relay, "HEARTBEAT", 0.2)  # varsayılan 20 sn: ölü kaydın düşmesini 5 sn'de göremeyiz
    rt = relays()
    r = relay_rig(rig, rt)
    r.start()
    until(lambda: registered(rt), 5, "relay kaydı")
    t0 = time.perf_counter()
    r.host.stop()
    assert time.perf_counter() - t0 < 0.25
    until(lambda: not registered(rt), 5, "kaydın düşmesi")
    until(lambda: [on for on, _ in sandbox.awake] == [True, False], 5, "uyku engelinin kalkması")
    assert len({t for _, t in sandbox.awake}) == 1
    assert sandbox.ngrok.calls == []  # relay kipinde ngrok'a hiç dokunulmaz


@pytest.mark.parametrize("addr, token", [("", ""), ("sunucu", ""), ("sunucu:abc", ""), ("sunucu:0", ""), ("sunucu:70000", ""),
                                         (":9000", ""), ("127.0.0.1:9000", "bir\nsatır")])
def test_relay_settings_are_validated_before_dialing(rig, monkeypatch, addr, token):
    monkeypatch.setattr(socket, "create_connection", tripwire("create_connection"))
    r = rig("relay", relay=addr, relay_token=token)
    r.host.start()
    until(lambda: r.ui.of("error"), 5, "hata")
    assert "sunucu:port" in r.ui.of("error")[0][0] and r.ui.of("ready") == []
    until(lambda: not any(t.name == "host-main" for t in threading.enumerate()), 5, "ana iş parçacığı")
