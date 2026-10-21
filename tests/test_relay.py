"""relay.py testleri.

Relay, test sürecinin içinde 127.0.0.1:0 üzerinde, kendi olay döngüsüyle arka plan iş
parçacığında çalışır; testler düz, bloklayan soketlerle konuşur.
"""
import ast
import asyncio
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import relay

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "s3cret-token"
HID = "123456789"


def dial(port):
    return socket.create_connection(("127.0.0.1", port), timeout=5)


class RelayThread:
    def __init__(self, token):
        self.relay = relay.Relay(token)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.socks = []
        self.port = self.call(self.relay.start("127.0.0.1", 0))

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def connect(self):
        sock = dial(self.port)
        self.socks.append(sock)
        return sock

    def stop(self):
        for sock in self.socks:
            sock.close()
        try:
            self.call(self.relay.stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            self.loop.close()


@pytest.fixture
def start_relay():
    # HEARTBEAT gibi sabitler relay başlamadan yamalanmalı: testler önce yamalar, sonra başlatır
    started = []

    def start(token=TOKEN):
        started.append(RelayThread(token))
        return started[-1]

    yield start
    for rt in started:
        rt.stop()


@pytest.fixture
def rt(start_relay):
    return start_relay()


def read_line(sock):
    """Bir satır (\\n dahil) okur; bağlantı kapanırsa o ana kadar gelenleri döner."""
    buf = bytearray()
    while not buf.endswith(b"\n"):
        try:
            ch = sock.recv(1)
        except ConnectionError:
            break
        if not ch:
            break
        buf += ch
    return bytes(buf)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def read_until_closed(sock, timeout=4):
    sock.settimeout(timeout)
    buf = bytearray()
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except ConnectionError:
        pass
    return bytes(buf)


def is_closed(sock, timeout=3):
    """Karşı taraf kapattı mı (EOF ya da reset); veri gelirse ya da süre dolarsa False."""
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except ConnectionError:
        return True
    except socket.timeout:
        return False


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def register(rt, hid=HID, token=TOKEN):
    sock = rt.connect()
    sock.sendall(("H %s %s\n" % (hid, token)).encode())
    assert read_line(sock) == b"OK\n"
    return sock


def join(rt, hid=HID, token=TOKEN):
    sock = rt.connect()
    sock.sendall(("C %s %s\n" % (hid, token)).encode())
    return sock, read_line(sock)


def ask(rt, raw):
    """Ham ilk satırı yollar; (relay'in cevabı, ardından bağlantı kapandı mı) döner."""
    sock = rt.connect()
    sock.sendall(raw)
    return read_line(sock), is_closed(sock)


def wait_go(host):
    # gerçek Host gibi: K satırları yok sayılır
    while True:
        line = read_line(host)
        if line != b"K\n":
            assert line == b"GO\n"
            return


def exchange(a, b, a_to_b, b_to_a):
    """İki yönü aynı anda yürütür (büyük veride tamponlar dolup kilitlenmesin).

    (b'nin aldığı, a'nın aldığı) döner."""
    senders = [threading.Thread(target=a.sendall, args=(a_to_b,)),
               threading.Thread(target=b.sendall, args=(b_to_a,))]
    for t in senders:
        t.start()
    got = (recv_exact(b, len(a_to_b)), recv_exact(a, len(b_to_a)))
    for t in senders:
        t.join()
    return got


# --- eşleşme ve boru -------------------------------------------------------

def test_pairing_pipes_arbitrary_binary_both_ways(rt):
    host = register(rt)
    client, reply = join(rt)
    assert reply == b"OK\n"
    assert read_line(host) == b"GO\n"
    # her bayt değeri (\n ve \0 dahil) iki yönde de bozulmadan ve sırayla geçmeli
    up = bytes(range(256)) * 1000 + os.urandom(200_000)
    down = os.urandom(300_000) + b"\n\n\x00\xff"
    assert exchange(client, host, up, down) == (up, down)
    # kayıt eşleşmede tükenir: aynı kimliğe ikinci Client düşer
    assert join(rt)[1] == b"ERR offline\n"


def test_bytes_right_after_the_preface_are_not_lost(rt):
    host = register(rt)
    client = rt.connect()
    client.sendall(("C %s %s\n" % (HID, TOKEN)).encode() + b"EARLY")   # OK'u beklemeden
    assert read_line(client) == b"OK\n"
    assert read_line(host) == b"GO\n"
    assert recv_exact(host, 5) == b"EARLY"


def test_many_concurrent_pairs_do_not_mix(rt):
    def one(i):
        hid = "%09d" % i
        host = register(rt, hid)
        client, reply = join(rt, hid)
        assert reply == b"OK\n"
        assert read_line(host) == b"GO\n"
        up, down = os.urandom(50_000 + i), os.urandom(50_000 + 3 * i)
        assert exchange(client, host, up, down) == (up, down)

    with ThreadPoolExecutor(max_workers=40) as pool:
        list(pool.map(one, range(40)))   # işçilerdeki assert'ler burada yeniden fırlar


@pytest.mark.parametrize("closer", ["host", "client"])
def test_either_side_closing_closes_the_other(rt, closer):
    host = register(rt)
    client, _ = join(rt)
    assert read_line(host) == b"GO\n"
    first, other = (host, client) if closer == "host" else (client, host)
    first.sendall(b"last words")   # kapanmadan önce yazılan da ulaşmalı
    first.close()
    assert recv_exact(other, 10) == b"last words"
    assert is_closed(other)
    assert wait_until(lambda: not rt.relay.tasks)   # sızan bağlantı görevi yok
    # relay sağlam: aynı kimlik yeniden eşleşebilir
    host2 = register(rt)
    assert join(rt)[1] == b"OK\n"
    assert read_line(host2) == b"GO\n"


# --- token ----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["H", "C"])
@pytest.mark.parametrize("token", ["wrong", "-", "", TOKEN[:-1], TOKEN + "x", TOKEN.upper()])
def test_wrong_token_gets_err_auth(rt, kind, token):
    assert ask(rt, ("%s %s %s\n" % (kind, HID, token)).encode()) == (b"ERR auth\n", True)
    assert HID not in rt.relay.waiting


def test_wrong_token_client_does_not_evict_waiting_host(rt):
    host = register(rt)
    assert ask(rt, ("C %s nope\n" % HID).encode()) == (b"ERR auth\n", True)
    client, reply = join(rt)
    assert reply == b"OK\n"
    assert read_line(host) == b"GO\n"


@pytest.mark.parametrize("token", ["şifre-é", "iki kelime", "ç" * 50])
def test_token_matches_byte_for_byte(start_relay, token):
    rt = start_relay(token)
    register(rt, token=token)   # boşluklu ve UTF-8 tokenlar da çalışır
    for raw in (token[:-1].encode(), token.encode() + b"x", b"\xff\xfe\x00"):
        assert ask(rt, b"H x " + raw + b"\n") == (b"ERR auth\n", True)
    register(rt, "y", token=token)   # geçersiz baytlar relay'i düşürmedi


def test_resolve_token_generates_when_omitted():
    a, b, c = relay.resolve_token(None), relay.resolve_token(""), relay.resolve_token(None)
    assert len({a, b, c}) == 3
    for token in (a, b, c):
        # boşluksuz ve satır sonsuz: ilk satıra güvenle yazılır; yeterince uzun
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", token)
    assert relay.resolve_token("verilen") == "verilen"
    with pytest.raises(ValueError):
        relay.Relay("")   # boş token ile relay hiç açılmaz


# --- bilinmeyen kimlik, değiştirme, ilk satır -------------------------------

def test_unknown_id_gets_err_offline(rt):
    assert ask(rt, ("C %s %s\n" % ("999999999", TOKEN)).encode()) == (b"ERR offline\n", True)


def test_new_registration_replaces_and_closes_the_old_one(rt):
    old = register(rt)
    new = register(rt)
    assert is_closed(old)   # GO da gelmedi: gerçekten EOF
    client, reply = join(rt)
    assert reply == b"OK\n"
    assert read_line(new) == b"GO\n"


@pytest.mark.parametrize("raw", [
    b"\n", b"hello\n", b"H\n", b"H abc\n", b"X abc tok\n", b"h abc tok\n", b" H abc tok\n",
    b"HH abc tok\n", b"H  tok\n", b"GET / HTTP/1.1\n", b"\xff\xfe\x00garbage\n",
])
def test_malformed_preface_gets_err_format(rt, raw):
    assert ask(rt, raw) == (b"ERR format\n", True)


@pytest.mark.parametrize("bad", [b"ABC", b"a/b", b"a\\b", "é".encode(), b"a;b", b"a" * 65, b"a\x00b"])
def test_bad_id_gets_err_format(rt, bad):
    for kind in (b"H", b"C"):
        assert ask(rt, kind + b" " + bad + b" " + TOKEN.encode() + b"\n") == (b"ERR format\n", True)


@pytest.mark.parametrize("good", ["1", HID, "a" * 64, "0.tcp.eu.ngrok.io:12345", "a_b-c.d:e"])
def test_good_id_is_accepted(rt, good):
    register(rt, good)


def test_preface_length_limit(rt):
    prefix = ("H %s " % HID).encode()
    at_limit = prefix + b"x" * (128 - len(prefix))
    assert len(at_limit) == 128
    assert ask(rt, at_limit + b"\n") == (b"ERR auth\n", True)       # biçim geçti, token yanlış
    assert ask(rt, at_limit + b"x\n") == (b"ERR format\n", True)    # 129 bayt
    assert ask(rt, b"A" * 300) == (b"ERR format\n", True)           # satır sonu hiç gelmiyor


def test_preface_flood_is_cut_and_relay_survives(rt):
    sock = rt.connect()

    def flood():
        try:
            sock.sendall(b"A" * 5_000_000)
        except OSError:
            pass   # relay kapattı

    t = threading.Thread(target=flood)
    t.start()
    t0 = time.monotonic()
    assert read_until_closed(sock) in (b"", b"ERR format\n")
    assert time.monotonic() - t0 < relay.PREFACE_TIMEOUT - 1   # zaman aşımını beklemeden kesildi
    t.join(10)
    register(rt)


def test_silent_and_too_slow_prefaces_time_out(monkeypatch, start_relay):
    monkeypatch.setattr(relay, "PREFACE_TIMEOUT", 0.6)
    rt = start_relay()
    socks = [rt.connect(), rt.connect()]
    socks[1].sendall(b"H 12345")   # biri hiç yazmıyor, öbürü yarım satırda takılıyor
    t0 = time.monotonic()
    for sock in socks:
        assert is_closed(sock, timeout=4)
    assert 0.4 <= time.monotonic() - t0 < 3   # hemen değil, süre dolunca
    # süre satırın TOPLAMI için geçerli: bayt damlatarak uzatılamaz
    sock = rt.connect()
    t0 = time.monotonic()
    for _ in range(15):
        try:
            sock.sendall(b"H")
        except OSError:
            break
        time.sleep(0.2)
    assert time.monotonic() - t0 < 2.4   # süre uygulanmasaydı 15 x 0.2 = 3 sn sürerdi
    assert is_closed(sock)


def test_fragmented_preface_within_the_timeout_is_accepted(monkeypatch, start_relay):
    monkeypatch.setattr(relay, "PREFACE_TIMEOUT", 0.6)
    rt = start_relay()
    sock = rt.connect()
    for chunk in (b"H 1234", b"56789 s3cret", b"-token\n"):
        sock.sendall(chunk)
        time.sleep(0.1)
    assert read_line(sock) == b"OK\n"


# --- kalp atışı, ölü Host, kapasite ----------------------------------------

def test_heartbeat_k_lines_reach_waiting_host(monkeypatch, start_relay):
    monkeypatch.setattr(relay, "HEARTBEAT", 0.2)
    rt = start_relay()
    host = register(rt)
    t0 = time.monotonic()
    assert [read_line(host) for _ in range(3)] == [b"K\n"] * 3
    assert 0.25 <= time.monotonic() - t0 < 5   # art arda yığılmadı, aralıklı geldi
    client, reply = join(rt)
    assert reply == b"OK\n"
    wait_go(host)
    # eşleşmeden sonra akışa K karışmaz
    time.sleep(0.7)
    client.sendall(b"payload")
    assert recv_exact(host, 7) == b"payload"
    host.settimeout(0.4)
    with pytest.raises(socket.timeout):
        host.recv(1)


@pytest.mark.parametrize("abortive", [False, True])
def test_dead_waiting_host_is_dropped_and_client_gets_offline(monkeypatch, start_relay, abortive):
    monkeypatch.setattr(relay, "HEARTBEAT", 0.2)
    rt = start_relay()
    host = register(rt)
    if abortive:   # FIN yerine RST: bağlantı aniden sıfırlanır
        host.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    host.close()
    assert wait_until(lambda: HID not in rt.relay.waiting)
    assert join(rt)[1] == b"ERR offline\n"
    # ayıklama kalp atışı görevini öldürmemiş olmalı
    assert read_line(register(rt, "other")) == b"K\n"


def test_client_gets_offline_even_before_the_next_heartbeat(rt):
    host = register(rt)   # HEARTBEAT 20 sn: sonraki atış çok uzakta
    host.close()
    time.sleep(0.3)
    client, reply = join(rt)
    assert reply == b"ERR offline\n"   # OK + hemen EOF değil
    assert HID not in rt.relay.waiting


def test_waiting_hosts_are_capped(monkeypatch, start_relay):
    monkeypatch.setattr(relay, "MAX_WAITING", 2)
    rt = start_relay()
    register(rt, "a")
    register(rt, "b")
    assert ask(rt, ("H c %s\n" % TOKEN).encode()) == (b"ERR full\n", True)
    register(rt, "a")   # var olan kimliği yenilemek kapasiteyi artırmaz
    assert join(rt, "b")[1] == b"OK\n"   # biri eşleşip çıkınca yer açılır
    register(rt, "c")


def test_accepted_sockets_are_tuned(rt):
    register(rt)
    sock = rt.relay.waiting[HID][1].get_extra_info("socket")
    assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)   # asyncio bunu Windows'ta kendisi açmıyor
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    if hasattr(socket, "TCP_KEEPIDLE"):   # en iyi çaba: yalnızca platform destekliyorsa ayarlı olmalı
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30


def test_stop_closes_listener_waiting_hosts_and_live_pairs(rt):
    waiting = register(rt, "a")
    host = register(rt, "b")
    client, _ = join(rt, "b")
    assert read_line(host) == b"GO\n"
    rt.call(rt.relay.stop())   # test soketlerini önce kapatmadan: relay kendi kapatmalı
    assert is_closed(waiting) and is_closed(host) and is_closed(client)
    assert not rt.relay.waiting and not rt.relay.tasks
    with pytest.raises(OSError):   # Windows'ta "reddedildi" ~2 sn sürer; açık dinleyici anında kabul ederdi
        socket.create_connection(("127.0.0.1", rt.port), timeout=0.3)


def test_unexpected_error_closes_only_that_connection(rt, monkeypatch, caplog):
    def boom(self, hid, reader, writer):
        raise RuntimeError("deneme hatası")

    monkeypatch.setattr(relay.Relay, "register", boom)
    # eski Python'larda yakalanmayan istisna soketi açık bırakırdı (fd sızıntısı)
    assert ask(rt, ("H %s %s\n" % (HID, TOKEN)).encode()) == (b"", True)
    assert "beklenmeyen hata" in caplog.text
    assert ask(rt, ("C %s %s\n" % (HID, TOKEN)).encode()) == (b"ERR offline\n", True)   # relay ayakta
    assert not rt.relay.tasks


# --- loglar ----------------------------------------------------------------

def test_logs_never_contain_tokens(start_relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    secret, wrong = "TOP-SECRET-TOKEN-1", "WRONG-TOKEN-2"
    rt = start_relay(secret)
    host = register(rt, token=secret)
    ask(rt, ("H %s %s\n" % (HID, wrong)).encode())
    ask(rt, ("C 1 %s\n" % wrong).encode())
    ask(rt, b"garbage with " + secret.encode() + b"\n")
    client, _ = join(rt, token=secret)
    assert read_line(host) == b"GO\n"
    client.close()
    assert wait_until(lambda: not rt.relay.tasks)
    assert caplog.records   # olaylar gerçekten loglanıyor
    assert secret not in caplog.text and wrong not in caplog.text


# --- komut satırı ----------------------------------------------------------

@pytest.mark.parametrize("given", [None, "cli-given-token"])
def test_cli_prints_token_and_serves(given):
    cmd = [sys.executable, "-u", str(ROOT / "relay.py"), "--port", "0"]
    # --host verilmezse varsayılan yalnızca loopback; verilirse o kullanılır
    cmd += ["--token", given, "--host", "127.0.0.1"] if given else []
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            encoding="utf-8")
    watchdog = threading.Timer(20, proc.kill)   # takılırsa okuma EOF ile biter
    watchdog.start()
    socks = []
    try:
        lines = []
        for line in proc.stdout:
            lines.append(line)
            if "portunu" in line:   # başlığın son satırı: güvenlik duvarı hatırlatması
                break
        out = "".join(lines)
        m = re.search(r"dinliyor: 127\.0\.0\.1:(\d+)\nToken: (\S+)\n.*TCP (\d+) portunu", out, re.S)
        assert m, out
        port, token = int(m.group(1)), m.group(2)
        assert m.group(3) == str(port)
        if given:
            assert token == given
        else:
            assert len(token) >= 16   # üretilen token ekrana yazıldı ve geçerli
        host = dial(port)
        socks.append(host)
        host.sendall(("H %s %s\n" % (HID, token)).encode())
        assert read_line(host) == b"OK\n"
        bad = dial(port)
        socks.append(bad)
        bad.sendall(("C %s wrong\n" % HID).encode())
        assert read_line(bad) == b"ERR auth\n"
        client = dial(port)
        socks.append(client)
        client.sendall(("C %s %s\n" % (HID, token)).encode())
        assert read_line(client) == b"OK\n"
        assert read_line(host) == b"GO\n"
    finally:
        watchdog.cancel()
        for sock in socks:
            sock.close()
        proc.kill()
        proc.wait(10)
        proc.stdout.close()


def test_main_reports_port_in_use(rt, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["relay.py", "--host", "127.0.0.1", "--port", str(rt.port)])
    with pytest.raises(SystemExit) as exc:
        relay.main()
    assert "Dinlenemedi" in str(exc.value)


# --- bağımlılık ve sözdizimi -----------------------------------------------

def test_relay_is_stdlib_only_and_python38_compatible():
    src = (ROOT / "relay.py").read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 8))   # match, except* vb. 3.8'de sözdizimi hatası
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
        for ann in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if ann is not None:   # X | Y ek açıklaması 3.8'de çalışmaz
                assert not any(isinstance(n, ast.BinOp) for n in ast.walk(ann))
    assert modules <= set(sys.stdlib_module_names)   # uzakmasa dahil hiçbir dış modül yok
    for newer in ("asyncio.timeout", "TaskGroup", "to_thread", "removeprefix", "removesuffix"):
        assert newer not in src
