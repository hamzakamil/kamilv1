"""proto.py testleri: SPEC kabul B (el sıkışma, çerçeveleme, saldırı senaryoları)."""
import hashlib
import os
import random
import socket
import sys
import threading
import time
import tracemalloc

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from uzakmasa import proto

PASSWORD = "doğru şifre deneme 1"
KEYS = (os.urandom(32), os.urandom(32))  # kanal testleri için sabit anahtar çifti (a->b, b->a)
P = 2**255 - 19
LOW_ORDER = [0, 1, 325606250916557431795983626356110631294008115727848805560023387167927233504,
             39382357235489614581723060781553021112529911719440698176882885853963445705823, P - 1, P, P + 1]


@pytest.fixture(scope="module")
def key():  # scrypt ~80 ms: modül başına bir kez
    return proto.derive_key(PASSWORD)


@pytest.fixture(scope="module")
def other_key():
    return proto.derive_key("yanlış şifre deneme 2")


# ---------- yardımcılar ----------


class FakeSock:
    """Bellek içi soket: rx hazır baytları verir, bitince EOF (0 bayt)."""

    def __init__(self, data=b""):
        self.rx = bytearray(data)
        self.tx = bytearray()

    def sendall(self, data):
        self.tx += data

    def recv_into(self, view):
        n = min(len(view), len(self.rx))
        view[:n] = self.rx[:n]
        del self.rx[:n]
        return n

    def shutdown(self, how):
        pass

    def close(self):
        pass


class DripSock(FakeSock):
    """Her okumada en çok 1 bayt verir: kısmi okuma yolunu zorlar."""

    def recv_into(self, view):
        return super().recv_into(view[:1])


class SlowSock(FakeSock):
    """sendall içinde iş parçacığı değişsin: kilitsiz gönderimde tel sırası burada bozulurdu."""

    def sendall(self, data):
        time.sleep(0)
        self.tx += data


class Tamper:
    """Araya giren saldırgan: alınan akışta `index` numaralı baytın bir bitini çevirir."""

    def __init__(self, sock, index):
        self._s, self._index, self._pos = sock, index, 0

    def __getattr__(self, name):
        return getattr(self._s, name)

    def recv_into(self, view):
        n = self._s.recv_into(view)
        if self._pos <= self._index < self._pos + n:
            view[self._index - self._pos] ^= 1
        self._pos += n
        return n


def sender(sock=None):
    sock = sock or FakeSock()
    return proto.Channel(sock, KEYS[0], KEYS[1]), sock


def receiver(data=b""):
    sock = FakeSock(data)
    return proto.Channel(sock, KEYS[1], KEYS[0]), sock


def frames_of(sock):
    raw, out = bytes(sock.tx), []
    while raw:
        n = 4 + int.from_bytes(raw[:4], "big")
        out.append(raw[:n])
        raw = raw[n:]
    return out


def hello(nid=b"123456789", pub=None, magic=proto.MAGIC, idlen=None):
    pub = pub or X25519PrivateKey.generate().public_key().public_bytes_raw()
    return magic + pub + os.urandom(16) + bytes([len(nid) if idlen is None else idlen]) + nid


@pytest.fixture
def pair():
    """Gerçek loopback soket çifti üretir; test bitince kapatır."""
    made = []

    def make():
        a, b = socket.socketpair()
        made.extend((a, b))
        return a, b

    yield make
    for s in made:
        s.close()


def serve(sock, my_id, key):
    """Sunucu el sıkışmasını ayrı iş parçacığında yürütür; sonuç box["ch"] ya da box["err"]."""
    box = {}

    def run():
        try:
            box["ch"] = proto.server_handshake(sock, my_id, key)
        except BaseException as e:
            box["err"] = e
            sock.close()  # host.py de reddettikten sonra kapatır

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def connect(pair, key_c, key_s=None, id_c="123456789", id_s="123456789"):
    c, s = pair()
    t, box = serve(s, id_s, key_s or key_c)
    try:
        ch_c = proto.client_handshake(c, id_c, key_c)
    finally:
        t.join(5)
    return ch_c, box["ch"], c, s


def run_client(pair, key, script, target="123456789"):
    """script(sunucu_soketi) sahte sunucudur; client_handshake sonucunu (Channel ya da istisna) döner."""
    c, s = pair()
    t = threading.Thread(target=script, args=(s,), daemon=True)
    t.start()
    try:
        return proto.client_handshake(c, target, key)
    except BaseException as e:
        return e
    finally:
        c.close()
        t.join(5)


# ---------- norm_id / derive_key / read_line / send_reject ----------


@pytest.mark.parametrize("raw, want", [
    ("123456789", "123456789"),
    (" 123 456 789 ", "123456789"),
    ("123-456-789", "123456789"),
    ("123 - 456 -789", "123456789"),
    ("0.TCP.eu.ngrok.io:12345", "0.tcp.eu.ngrok.io:12345"),
    ("  0.tcp.eu.ngrok.io:12345\n", "0.tcp.eu.ngrok.io:12345"),
    ("0.tcp.in-1.ngrok.io:1234", "0.tcp.in-1.ngrok.io:1234"),  # tire kalır
    ("ab c-d", "abc-d"),
    ("", ""),
    ("١٢٣", "١٢٣"),  # ASCII olmayan rakamlar röle kimliği sayılmaz
])
def test_norm_id(raw, want):
    assert proto.norm_id(raw) == want
    assert proto.norm_id(want) == want  # tekrar uygulamak değiştirmez


def test_derive_key_pins_parameters(key, other_key):
    want = hashlib.scrypt(PASSWORD.encode("utf-8"), salt=b"uzakmasa-v1", n=2**15, r=8, p=1, dklen=32,
                          maxmem=128 * 1024 * 1024)
    assert key == want and len(key) == 32
    assert key != other_key


def test_read_line_does_not_overread(pair):
    a, b = pair()
    a.sendall(b"OK\nGO\nikili")
    assert proto.read_line(b) == "OK"
    assert proto.read_line(b) == "GO"
    assert proto._read_exact(b, 5) == b"ikili"


def test_read_line_drip_and_empty(pair):
    a, b = pair()

    def drip():
        for ch in b"OK 12\n\n":
            a.sendall(bytes([ch]))
            time.sleep(0.003)

    threading.Thread(target=drip, daemon=True).start()
    assert proto.read_line(b) == "OK 12"
    assert proto.read_line(b) == ""


def test_read_line_errors(pair):
    a, b = pair()
    a.sendall(b"y" * 128 + b"\n")
    assert proto.read_line(b) == "y" * 128  # tam sınır kabul
    a.sendall(b"x" * 500)
    with pytest.raises(ValueError):
        proto.read_line(b)
    assert proto._read_exact(b, 500 - 129) == b"x" * (500 - 129)  # 129. bayttan sonra okumayı bıraktı
    a.sendall(b"\xff\xfe\n")
    with pytest.raises(ValueError):  # geçersiz utf-8
        proto.read_line(b)
    a.sendall(b"yarim")
    a.close()
    with pytest.raises(ConnectionError):
        proto.read_line(b)


def test_send_reject_wire_and_never_raises(pair):
    a, b = pair()
    proto.send_reject(a, b"B", linger=0)
    assert proto._read_exact(b, 5) == proto.REJECT + b"B"
    a.close()
    proto.send_reject(a, b"B")  # kapalı soket: sessizce döner
    b.close()
    proto.send_reject(a, b"P")
    proto.send_reject(object(), b"P")  # soket bile değilse de


def test_send_reject_reaches_peer_even_if_hello_unread(pair):
    # meşgul reddi hello okunmadan gider; sade close() burada REJECT'i RST ile yutardı (ölçüm: 0/20)
    for _ in range(10):
        c, s = pair()

        def host():
            proto.send_reject(s, b"B")
            s.close()

        t = threading.Thread(target=host, daemon=True)
        t.start()
        c.sendall(hello())
        c.settimeout(2)
        assert proto._read_exact(c, 5) == proto.REJECT + b"B"
        c.close()  # karşının kapatması bekleyişi bitirir
        t.join(2)
        assert not t.is_alive()


# ---------- Channel: çerçeve biçimi ve saldırılar (bellek içi soket) ----------


def test_frame_format_and_roundtrip_sizes():
    a, sa = sender()
    b, sb = receiver()
    b.trust()
    payloads = [b"", b"x", os.urandom(100), os.urandom(4000), os.urandom(70_000), os.urandom(1_000_000)]
    for p in payloads:
        a.send(b"F", p)
    frames = frames_of(sa)
    for f, p in zip(frames, payloads):
        assert int.from_bytes(f[:4], "big") == len(f) - 4 == 1 + len(p) + 16
    assert a.bytes_sent == len(sa.tx)
    sb.rx += sa.tx
    for p in payloads:
        assert b.recv() == (b"F", p)


def test_same_plaintext_encrypts_differently_each_frame():
    a, sa = sender()
    a.send(b"P")
    a.send(b"P")
    f1, f2 = frames_of(sa)
    assert f1 != f2  # sayaç nonce'u her çerçevede ilerler
    b, _ = receiver(f1 + f2)
    assert b.recv() == (b"P", b"") and b.recv() == (b"P", b"")


def test_replayed_frame_rejected():
    a, sa = sender()
    a.send(b"I", b"1")
    (f,) = frames_of(sa)
    b, _ = receiver(f + f)
    assert b.recv() == (b"I", b"1")
    with pytest.raises(proto.AuthError):
        b.recv()


def test_reordered_and_dropped_frames_rejected():
    for order in ([1, 0], [1]):  # ters sıra; ilk çerçeve kayıp
        a, sa = sender()
        a.send(b"I", b"1")
        a.send(b"I", b"2")
        f = frames_of(sa)
        b, _ = receiver(b"".join(f[i] for i in order))
        with pytest.raises(proto.AuthError):
            b.recv()


def test_reflected_frame_rejected():
    a, sa = sender()
    a.send(b"I", b"1")
    (f,) = frames_of(sa)
    sa.rx += f  # kendi çerçevesi geri yansıtılır: yön anahtarları farklı
    with pytest.raises(proto.AuthError):
        a.recv()


def test_every_bit_flip_rejected():
    a, sa = sender()
    a.send(b"I", b"payload")
    (f,) = frames_of(sa)
    for i in range(len(f)):
        for bit in (0x01, 0x80):
            bad = bytearray(f)
            bad[i] ^= bit
            b, _ = receiver(bytes(bad))
            # uzunluk baytları bozulursa gövde eksik/fazla kalabilir (EOF); hiçbir durumda kabul edilmez
            with pytest.raises((proto.AuthError, ConnectionError) if i < 4 else proto.AuthError):
                b.recv()


def test_garbage_rejected():
    rng = random.Random(1234)
    for _ in range(200):
        b, _ = receiver(rng.randbytes(rng.randrange(1, 300)))
        with pytest.raises((proto.AuthError, ConnectionError)):
            b.recv()
    b, _ = receiver((64).to_bytes(4, "big") + rng.randbytes(64))  # geçerli başlık, rastgele gövde
    with pytest.raises(proto.AuthError):
        b.recv()


def test_oversize_length_rejected_before_reading_body():
    # gövde hiç gönderilmiyor: okumaya kalkışsa AuthError yerine ConnectionError (EOF) çıkardı
    for n in (proto.PRE_AUTH_MAX + 1, 10**6, 0xFFFFFFFE, 16, 0):
        b, _ = receiver(n.to_bytes(4, "big"))
        with pytest.raises(proto.AuthError):
            b.recv()
    b, _ = receiver((proto.MAX_FRAME + 1).to_bytes(4, "big"))
    b.trust()
    with pytest.raises(proto.AuthError):
        b.recv()
    # sınır değerleri kabul edilir: uzunluk geçer, gövde gelmediği için EOF
    for n, trusted in ((17, False), (proto.PRE_AUTH_MAX, False), (proto.MAX_FRAME, True)):
        b, _ = receiver(n.to_bytes(4, "big"))
        if trusted:
            b.trust()
        with pytest.raises(ConnectionError):
            b.recv()


def test_oversize_length_allocates_nothing():
    b, _ = receiver((0xFFFFFFFE).to_bytes(4, "big"))
    tracemalloc.start()
    try:
        with pytest.raises(proto.AuthError):
            b.recv()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1 << 20


@pytest.mark.parametrize("reason", ["P", "I", "B", "K"])
def test_reject_reasons(reason):
    b, _ = receiver(proto.REJECT + reason.encode())
    with pytest.raises(proto.AuthError) as ei:
        b.recv()
    assert ei.value.reason == reason


def test_reject_unknown_reason_and_missing_reason_byte():
    for r in (b"Z", b"\x00", b"\xff"):
        b, _ = receiver(proto.REJECT + r)
        with pytest.raises(proto.AuthError) as ei:
            b.recv()
        assert ei.value.reason is None
    b, _ = receiver(proto.REJECT)  # sebep baytı hiç gelmedi
    with pytest.raises(ConnectionError):
        b.recv()


def test_pre_auth_max_enforced_until_trust():
    big = bytes(proto.PRE_AUTH_MAX)  # şifreli uzunluk 4113 > 4096
    a, sa = sender()
    a.send(b"F", big)
    (f,) = frames_of(sa)
    b, _ = receiver(f)
    with pytest.raises(proto.AuthError):
        b.recv()
    b, _ = receiver(f)
    b.trust()
    assert b.recv() == (b"F", big)
    exact = bytes(proto.PRE_AUTH_MAX - 17)  # şifreli uzunluk tam 4096: kabul
    a, sa = sender()
    a.send(b"F", exact)
    b, _ = receiver(bytes(sa.tx))
    assert b.recv() == (b"F", exact)


def test_partial_reads_and_eof_midframe():
    a, sa = sender()
    a.send(b"I", b"x" * 300)
    (f,) = frames_of(sa)
    b = proto.Channel(DripSock(f), KEYS[1], KEYS[0])  # bayt bayt gelir
    assert b.recv() == (b"I", b"x" * 300)
    for cut in (0, 2, 4, 10, len(f) - 1):  # çerçeve ortasında ya da sınırında kopma
        b, _ = receiver(f[:cut])
        with pytest.raises(ConnectionError):
            b.recv()


def test_partial_reads_over_real_socket(pair):
    a_sock, b_sock = pair()
    a, sa = sender()
    a.send(b"I", b"y" * 200)
    (f,) = frames_of(sa)

    def drip():
        for i in range(0, len(f), 7):
            a_sock.sendall(f[i:i + 7])
            time.sleep(0.002)

    threading.Thread(target=drip, daemon=True).start()
    b = proto.Channel(b_sock, KEYS[1], KEYS[0])
    assert b.recv() == (b"I", b"y" * 200)


def test_socket_timeout_propagates(pair):
    _, b_sock = pair()
    b_sock.settimeout(0.1)
    b = proto.Channel(b_sock, KEYS[1], KEYS[0])
    t0 = time.perf_counter()
    with pytest.raises(socket.timeout):
        b.recv()
    assert time.perf_counter() - t0 < 2


# ---------- Channel: eşzamanlı gönderim, kilit, close ----------


def test_wire_order_equals_counter_order():
    a, sock = sender(SlowSock())
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def burst(tid):
            for i in range(100):
                a.send(b"I", bytes([tid, i]))

        threads = [threading.Thread(target=burst, args=(t,)) for t in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        sys.setswitchinterval(old)
    b, _ = receiver(bytes(sock.tx))
    seen = {0: [], 1: []}
    for _ in range(200):
        _, p = b.recv()  # sıra bozulsaydı burada AuthError çıkardı
        seen[p[0]].append(p[1])
    assert seen[0] == seen[1] == list(range(100))


def test_two_threads_500_concurrent_sends_over_socket(pair):
    a_sock, b_sock = pair()
    snd = proto.Channel(a_sock, KEYS[0], KEYS[1])
    rcv = proto.Channel(b_sock, KEYS[1], KEYS[0])
    n, errors, got = 500, [], {0: [], 1: []}

    def producer(tid):
        rng = random.Random(tid)
        try:
            for seq in range(n):
                snd.send(b"I", bytes([tid]) + seq.to_bytes(2, "big") + bytes(rng.randrange(0, 3000)))
        except BaseException as e:
            errors.append(e)

    def consumer():
        try:
            for _ in range(2 * n):
                _, p = rcv.recv()
                got[p[0]].append(int.from_bytes(p[1:3], "big"))
        except BaseException as e:
            errors.append(e)
            rcv.close()  # hata varsa üreticiler sendall'da asılı kalmasın

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=consumer, daemon=True)]
        threads += [threading.Thread(target=producer, args=(t,), daemon=True) for t in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        sys.setswitchinterval(old)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert got[0] == got[1] == list(range(n))


def test_close_wakes_blocked_recv_within_1s(pair):
    a_sock, b_sock = pair()
    b_sock.settimeout(30)  # zaman aşımı değil, close() uyandırmalı
    ch = proto.Channel(b_sock, KEYS[1], KEYS[0])
    out = []

    def reader():
        try:
            ch.recv()
        except BaseException as e:
            out.append(e)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.3)
    t0 = time.perf_counter()
    ch.close()
    t.join(1.0)
    assert not t.is_alive()
    assert time.perf_counter() - t0 < 1.0
    assert isinstance(out[0], OSError)


def test_close_is_idempotent_from_any_thread_and_send_after_close_fails(pair):
    a_sock, b_sock = pair()
    ch = proto.Channel(a_sock, KEYS[0], KEYS[1])
    threads = [threading.Thread(target=ch.close) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    ch.close()
    ch.close(linger=0.1)
    with pytest.raises(OSError):
        ch.send(b"P")
    with pytest.raises(OSError):
        ch.recv()


@pytest.mark.parametrize("linger", [0, 0.3])
def test_stalled_peer_lock_timeout_then_close_wakes_blocked_send(pair, linger):
    a_sock, _ = pair()  # karşı taraf hiç okumaz
    ch = proto.Channel(a_sock, KEYS[0], KEYS[1])
    errs = []

    def flood():
        try:
            for _ in range(200):
                ch.send(b"F", bytes(262144))
        except OSError as e:
            errs.append(e)

    t = threading.Thread(target=flood, daemon=True)
    t.start()
    time.sleep(0.7)  # tamponlar dolsun, sendall kilidi tutarak bloklansın
    assert t.is_alive()
    watchdog = threading.Timer(5, ch.close)  # kilit bozuksa test asılı kalmasın
    watchdog.start()
    try:
        t0 = time.perf_counter()
        with pytest.raises(TimeoutError):
            ch.send(b"P", lock_timeout=0.2)
        assert 0.15 <= time.perf_counter() - t0 < 1.0
    finally:
        watchdog.cancel()
    t0 = time.perf_counter()
    ch.close(linger=linger)  # kilit almaz: bloklu göndereni uyandırır (linger en çok o kadar bekletir)
    t.join(1.0)
    assert not t.is_alive() and errs
    assert time.perf_counter() - t0 < 1.0


def test_close_with_linger_delivers_last_frame_to_busy_peer(pair, key):
    # Q'dan hemen sonra sade close(), karşı taraf veri gönderirken Q'yu RST ile yutar (ölçüm: 0/15)
    def one_round():
        cc, sc, _, _ = connect(pair, key)
        sc.trust()
        cc.trust()
        stop, got = threading.Event(), []

        def pump():
            try:
                while not stop.is_set():
                    cc.send(b"A")
                    time.sleep(0.001)
            except OSError:
                pass

        def client():
            try:
                while True:
                    if cc.recv()[0] == b"Q":
                        got.append("Q")
                        return
            except OSError as e:
                got.append(type(e).__name__)
            finally:
                stop.set()
                cc.close()

        threading.Thread(target=pump, daemon=True).start()
        ct = threading.Thread(target=client, daemon=True)
        ct.start()
        time.sleep(0.05)
        sc.send(b"Q")
        sc.close(linger=0.5)
        ct.join(3)
        return got

    for _ in range(5):
        assert one_round() == ["Q"]


# ---------- el sıkışma (gerçek soket çifti) ----------


def test_handshake_roundtrip_both_directions(pair, key):
    cc, sc, c, s = connect(pair, key, id_c="123 456 789", id_s="123456789")
    assert (c.gettimeout(), s.gettimeout()) == (proto.HANDSHAKE_TIMEOUT,) * 2
    for sk in (c, s):
        assert sk.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assert sk.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    cc.send(b"H", '{"uid":"ş"}'.encode("utf-8"))
    assert sc.recv() == (b"H", '{"uid":"ş"}'.encode("utf-8"))
    sc.trust()
    big = os.urandom(200_000)
    sc.send(b"W", b"{}")
    sc.send(b"F", big)
    assert cc.recv() == (b"W", b"{}")
    cc.trust()
    assert cc.recv() == (b"F", big)
    assert sc.bytes_sent == (4 + 1 + 2 + 16) + (4 + 1 + len(big) + 16)
    assert cc.bytes_sent == 4 + 1 + len('{"uid":"ş"}'.encode("utf-8")) + 16


@pytest.mark.parametrize("dial, mine", [
    ("123 456 789", "123456789"),
    ("123-456-789", "123 456 789"),
    ("0.TCP.EU.NGROK.IO:12345", "0.tcp.eu.ngrok.io:12345"),
    ("  0.tcp.in-1.ngrok.io:5 ", "0.tcp.in-1.ngrok.io:5"),
])
def test_ids_match_after_normalization(pair, key, dial, mine):
    cc, sc, _, _ = connect(pair, key, id_c=dial, id_s=mine)
    cc.send(b"P")
    assert sc.recv() == (b"P", b"")


def test_pre_auth_max_over_real_handshake(pair, key):
    cc, sc, _, _ = connect(pair, key)
    cc.send(b"H", bytes(5000))  # ilk doğrulanmış çerçeveden önce 4096 sınırı
    with pytest.raises(proto.AuthError):
        sc.recv()
    cc, sc, _, _ = connect(pair, key)
    cc.send(b"H", b"{}")
    assert sc.recv() == (b"H", b"{}")
    sc.trust()
    cc.send(b"I", bytes(5000))
    assert sc.recv() == (b"I", bytes(5000))


def test_wrong_password_client_sees_P_host_sees_auth_error(pair, key, other_key):
    cc, sc, c, s = connect(pair, key, other_key)  # istemci parolanın yanlış olduğunu henüz bilmez
    cc.send(b"H", b'{"uid":"x"}')
    with pytest.raises(proto.AuthError) as ei:
        sc.recv()  # host: ilk çerçeve çözülemez
    assert ei.value.reason is None
    proto.send_reject(s, b"P", linger=0)  # host.py'nin yaptığı
    s.close()
    with pytest.raises(proto.AuthError) as ei:
        cc.recv()
    assert ei.value.reason == "P"


def test_wrong_password_stream_unreadable_both_ways(pair, key, other_key):
    cc, sc, _, _ = connect(pair, key, other_key)
    sc.send(b"W", b"{}")
    with pytest.raises(proto.AuthError):
        cc.recv()
    cc, sc, _, _ = connect(pair, other_key, key)
    cc.send(b"H", b"{}")
    with pytest.raises(proto.AuthError):
        sc.recv()


def test_wrong_id_client_sees_I_and_host_raises(pair, key):
    c, s = pair()
    t, box = serve(s, "123456789", key)
    with pytest.raises(proto.AuthError) as ei:
        proto.client_handshake(c, "987654321", key)
    assert ei.value.reason == "I"
    t.join(5)
    assert isinstance(box["err"], proto.AuthError) and box["err"].reason == "I"


@pytest.mark.parametrize("reason", ["P", "I", "B", "K"])
def test_client_handshake_handles_reject_with_reason(pair, key, reason):
    def script(s):
        proto._read_exact(s, 61)
        proto.send_reject(s, reason.encode(), linger=0)
        s.close()

    err = run_client(pair, key, script)
    assert isinstance(err, proto.AuthError) and err.reason == reason


def test_busy_reject_before_hello_reaches_client(pair, key):
    # host meşgulken hello'yu okumadan reddeder; istemci yine de B'yi görmeli
    for _ in range(10):
        def script(s):
            proto.send_reject(s, b"B")
            s.close()

        err = run_client(pair, key, script)
        assert isinstance(err, proto.AuthError) and err.reason == "B"


def test_client_handshake_other_failures(pair, key):
    def closes(s):
        proto._read_exact(s, 61)
        s.close()

    assert isinstance(run_client(pair, key, closes), ConnectionError)

    def low_order_reply(s):
        proto._read_exact(s, 61)
        s.sendall(bytes(48))  # sıfır açık anahtar
        time.sleep(0.2)
        s.close()

    err = run_client(pair, key, low_order_reply)
    assert isinstance(err, proto.AuthError)
    for bad in ("", "   ", "x" * 149):  # boş ya da 200 baytlık hello'ya sığmayan ID: ağa çıkmadan reddedilir
        c, s = pair()
        with pytest.raises(proto.AuthError) as ei:
            proto.client_handshake(c, bad, key)
        assert ei.value.reason == "I"
        s.settimeout(0.1)
        with pytest.raises(socket.timeout):
            s.recv(1)  # tek bayt bile gönderilmedi


def test_client_hello_wire_format(pair, key):
    c, s = pair()
    box = {}

    def run():
        try:
            proto.client_handshake(c, " 0.TCP.eu.ngrok.io:12345 ", key)
        except BaseException as e:
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    head = proto._read_exact(s, 52)
    nid = proto._read_exact(s, head[51])
    assert head[:3] == proto.MAGIC
    assert nid == b"0.tcp.eu.ngrok.io:12345"
    assert len(head) + len(nid) <= 200
    s.close()
    t.join(5)
    assert isinstance(box["err"], ConnectionError)


def _server_vs_raw_client(pair, key, data, my_id="123456789"):
    """Ham baytları sunucuya yollar; (sunucu hatası, istemcinin okuduğu 5 bayt) döner."""
    c, s = pair()
    t, box = serve(s, my_id, key)
    c.sendall(data)
    c.settimeout(3)
    reply = proto._read_exact(c, 5)
    t.join(5)
    assert not t.is_alive()
    return box["err"], reply


def test_server_rejects_bad_magic(pair, key):
    err, reply = _server_vs_raw_client(pair, key, b"GET")  # sunucunun okuduğu kadar: RST olmasın
    assert isinstance(err, proto.AuthError) and err.reason == "I"
    assert reply == proto.REJECT + b"I"


def test_server_rejects_bad_utf8_id_and_oversize_idlen(pair, key):
    err, reply = _server_vs_raw_client(pair, key, hello(nid=b"\xff\xfe"))
    assert isinstance(err, proto.AuthError) and reply == proto.REJECT + b"I"
    t0 = time.perf_counter()  # ID baytları hiç gelmiyor: beklemeden reddetmeli
    err, reply = _server_vs_raw_client(pair, key, hello(nid=b"", idlen=255))
    assert isinstance(err, proto.AuthError) and reply == proto.REJECT + b"I"
    assert time.perf_counter() - t0 < 2


def test_server_rejects_wrong_id_even_if_normalized_close(pair, key):
    err, reply = _server_vs_raw_client(pair, key, hello(nid=b"123456780"))
    assert isinstance(err, proto.AuthError) and reply == proto.REJECT + b"I"


@pytest.mark.parametrize("u", LOW_ORDER + [v ^ (1 << 255) for v in LOW_ORDER])
def test_low_order_client_key_rejected(pair, key, u):
    err, reply = _server_vs_raw_client(pair, key, hello(pub=u.to_bytes(32, "little")))
    assert isinstance(err, proto.AuthError) and reply == proto.REJECT + b"I"


class ZeroDH:
    """Her zaman sıfır ortak sır üreten sahte X25519: kütüphane reddetmese bile kontrol çalışmalı."""

    def __init__(self):
        self._real = X25519PrivateKey.generate()

    @classmethod
    def generate(cls):
        return cls()

    def public_key(self):
        return self._real.public_key()

    def exchange(self, peer):
        return bytes(32)


def test_all_zero_shared_secret_rejected_on_both_sides(pair, key, monkeypatch):
    monkeypatch.setattr(proto, "X25519PrivateKey", ZeroDH)
    err, reply = _server_vs_raw_client(pair, key, hello())
    assert isinstance(err, proto.AuthError) and reply == proto.REJECT + b"I"

    def honest_reply(s):
        proto._read_exact(s, 61)
        s.sendall(X25519PrivateKey.generate().public_key().public_bytes_raw() + os.urandom(16))
        time.sleep(0.2)

    assert isinstance(run_client(pair, key, honest_reply), proto.AuthError)


def test_server_truncated_or_silent_hello(pair, key, monkeypatch):
    c, s = pair()
    t, box = serve(s, "123456789", key)
    c.sendall(hello()[:10])
    c.close()
    t.join(5)
    assert isinstance(box["err"], ConnectionError)
    monkeypatch.setattr(proto, "HANDSHAKE_TIMEOUT", 0.3)
    c, s = pair()
    t0 = time.perf_counter()
    t, box = serve(s, "123456789", key)
    t.join(5)  # istemci hiçbir şey göndermez
    assert isinstance(box["err"], TimeoutError) and time.perf_counter() - t0 < 3


@pytest.mark.parametrize("index", [0, 31, 32, 47])
def test_tampered_server_reply_breaks_first_frame(pair, key, index):
    c, s = pair()
    t, box = serve(s, "123456789", key)
    cc = proto.client_handshake(Tamper(c, index), "123456789", key)  # istemci fark edemez
    t.join(5)
    cc.send(b"H", b"{}")
    with pytest.raises(proto.AuthError):
        box["ch"].recv()


@pytest.mark.parametrize("index", [3, 34, 35, 50])
def test_tampered_client_hello_breaks_first_frame(pair, key, index):
    c, s = pair()
    t, box = serve(Tamper(s, index), "123456789", key)
    cc = proto.client_handshake(c, "123456789", key)
    t.join(5)
    cc.send(b"H", b"{}")
    with pytest.raises(proto.AuthError):
        box["ch"].recv()


def test_id_is_bound_into_key_schedule(pair, key):
    # araya giren saldırgan hello'daki ID'yi başka bir Host'unkiyle değiştirir (son rakam 9 -> 8): anahtarlar tutmamalı
    c, s = pair()
    t, box = serve(Tamper(s, 60), "123456788", key)
    cc = proto.client_handshake(c, "123456789", key)
    t.join(5)
    cc.send(b"H", b"{}")
    with pytest.raises(proto.AuthError):
        box["ch"].recv()


def test_sessions_do_not_share_keys_and_cross_session_replay_fails(pair, key):
    captured = []

    class Tap:
        def __init__(self, sock):
            self._s = sock

        def __getattr__(self, name):
            return getattr(self._s, name)

        def sendall(self, data):
            captured.append(bytes(data))
            self._s.sendall(data)

    frames = []
    for _ in range(2):
        c, s = pair()
        t, box = serve(s, "123456789", key)
        cc = proto.client_handshake(Tap(c), "123456789", key)
        t.join(5)
        cc.send(b"H", b'{"uid":"ayni"}')
        assert box["ch"].recv()[0] == b"H"
        frames.append(captured[-1])
    assert frames[0] != frames[1]  # aynı şifre ve düz metin, farklı oturum: farklı şifreli metin
    c, s = pair()  # 3. oturum: eski oturumdan kaydedilmiş ilk çerçeve
    t, box = serve(s, "123456789", key)
    proto.client_handshake(c, "123456789", key)
    t.join(5)
    c.sendall(frames[0])
    with pytest.raises(proto.AuthError):
        box["ch"].recv()


def test_empty_or_short_key_refused(pair, key):
    for bad in (b"", b"kisa", bytes(31), bytes(33)):  # boş anahtar parolasız el sıkışma olurdu
        with pytest.raises(ValueError):
            proto.client_handshake(None, "1", bad)
        with pytest.raises(ValueError):
            proto.server_handshake(None, "1", bad)
