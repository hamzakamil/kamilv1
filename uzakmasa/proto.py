"""El sıkışma ve şifreli çerçeveleme: X25519 + HKDF(parola anahtarı) + ChaCha20-Poly1305. PAKE değildir (README)."""
import hashlib
import hmac
import os
import socket
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"UM1"
MAX_FRAME = 8 * 1024 * 1024
PRE_AUTH_MAX = 4096
REJECT = b"\xff\xff\xff\xff"  # arkasından TEK sebep baytı gelir
# AuthError.reason değerleri; istemci bunları kendi hata koduna çevirir
REASONS = {"P": "yanlış şifre", "I": "yanlış ID", "B": "meşgul", "K": "atıldı"}
HANDSHAKE_TIMEOUT = 5
LINGER = 0.5  # nazik kapanışta karşı tarafın kapatmasını en çok bu kadar bekle
_HELLO_FIXED = 52  # MAGIC(3) + açık anahtar(32) + nonce(16) + idlen(1)
_HELLO_MAX = 200


class AuthError(Exception):
    """Yanlış şifre/ID, bozuk ya da oynanmış veri, karşı tarafın reddi. reason: 1 karakter ya da None."""

    def __init__(self, msg: str = "kimlik doğrulama hatası", reason: str | None = None):
        super().__init__(msg)
        self.reason = reason


def norm_id(s: str) -> str:
    s = "".join(s.lower().split())
    digits = s.replace("-", "")
    # ngrok bölge adlarında tire var, o yüzden yalnızca salt rakam kimliklerde tire atılır
    return digits if digits.isascii() and digits.isdigit() else s


def derive_key(password: str) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=b"uzakmasa-v1", n=2**15, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024
    )


def _read_exact(sock, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        got = sock.recv_into(view[pos:])
        if not got:
            raise ConnectionError("bağlantı kapandı")
        pos += got
    return bytes(buf)


def read_line(sock, maxlen: int = 128) -> str:
    # bayt bayt: röle ön yazısından sonra ikili veri gelir, fazla okumamalıyız
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            raise ConnectionError("bağlantı kapandı")
        if c == b"\n":
            return buf.decode("utf-8")
        buf += c
        if len(buf) > maxlen:
            raise ValueError("satır çok uzun")


def _drain(sock, seconds: float) -> None:
    # FIN yolla, karşı taraf kapatana kadar (en çok seconds) verisini boşalt. Okunmamış veriyle close()
    # RST yollar ve az önce yazılan REJECT/Q baytlarını yutar (Windows loopback: 0/15 ulaştı, bununla 15/15)
    try:
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            sock.settimeout(left)
            if not sock.recv(4096):
                return
    except Exception:
        pass


def send_reject(sock, reason: bytes, linger: float = LINGER) -> None:
    # hello okunmadan reddedilirse (meşgul) bekleyiş şart; her çağrıdan sonra çağıran close() eder
    data = REJECT + reason
    try:
        sock.sendall(data)
    except Exception:  # en iyi çaba: karşı taraf zaten gitmiş olabilir
        return
    if linger > 0:
        _drain(sock, linger)


def _rejected(sock) -> AuthError:
    r = _read_exact(sock, 1).decode("latin-1")
    return AuthError("karşı taraf reddetti: " + REASONS.get(r, "bilinmeyen sebep"), r if r in REASONS else None)


class Channel:
    def __init__(self, sock, send_key: bytes, recv_key: bytes, max_len: int = PRE_AUTH_MAX):
        self._sock = sock
        self._aead_send = ChaCha20Poly1305(send_key)
        self._aead_recv = ChaCha20Poly1305(recv_key)
        self._send_n = 0
        self._recv_n = 0
        self._lock = threading.Lock()
        self.max_len = max_len
        self.bytes_sent = 0

    def send(self, kind: bytes, payload: bytes = b"", lock_timeout: float | None = None) -> None:
        if not self._lock.acquire(timeout=-1 if lock_timeout is None else lock_timeout):
            raise TimeoutError("gönderim kilidi alınamadı")
        # tek kilit: tel sırası sayaç sırasıyla aynı olmalı, yoksa alıcıda etiket hatası çıkar
        try:
            body = kind + payload
            head = (len(body) + 16).to_bytes(4, "big")
            nonce = bytes(4) + self._send_n.to_bytes(8, "big")
            self._send_n += 1  # gönderim patlasa da sayaç harcanır: nonce asla tekrar kullanılmaz
            frame = head + self._aead_send.encrypt(nonce, body, head)
            self._sock.sendall(frame)
            self.bytes_sent += len(frame)
        finally:
            self._lock.release()

    def recv(self) -> tuple[bytes, bytes]:
        head = _read_exact(self._sock, 4)
        if head == REJECT:
            raise _rejected(self._sock)
        n = int.from_bytes(head, "big")
        # uzunluk, bellek ayrılmadan ve gövde okunmadan ÖNCE doğrulanır
        if not 17 <= n <= self.max_len:
            raise AuthError("geçersiz çerçeve uzunluğu")
        nonce = bytes(4) + self._recv_n.to_bytes(8, "big")
        try:
            plain = self._aead_recv.decrypt(nonce, _read_exact(self._sock, n), head)
        except InvalidTag:
            raise AuthError("çözülemedi: yanlış şifre ya da bozuk veri") from None
        self._recv_n += 1
        return plain[:1], plain[1:]

    def trust(self) -> None:
        self.max_len = MAX_FRAME

    def close(self, linger: float = 0) -> None:
        # kilit almaz; Windows'ta bloklu recv/send'i yalnızca close() uyandırır.
        # linger>0 yalnızca son çerçevenin (Q, D) ulaşması gereken kapanışlarda: gönderen iş parçacığını bekletir
        if linger > 0:
            _drain(self._sock, linger)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _prepare(sock, key: bytes) -> None:
    # boş anahtar HKDF'te tuzsuz çalışır = parolasız el sıkışma; asla izin verme
    if len(key) != 32:
        raise ValueError("anahtar 32 bayt olmalı")
    # shortcut: zaman aşımı her okuma içindir, toplam süre değil (baytları damlatan biri 8 ön-doğrulama yuvasından birini tutar), yükseltme: mutlak son tarih
    sock.settimeout(HANDSHAKE_TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def _keys(priv, peer_pub: bytes, key: bytes, nid: bytes, pub_c: bytes, pub_h: bytes, nonce_c: bytes, nonce_h: bytes):
    """(c2h, h2c) anahtarları. Geçersiz ya da düşük dereceli noktada AuthError."""
    try:
        shared = priv.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    except ValueError:
        raise AuthError("geçersiz açık anahtar") from None
    if hmac.compare_digest(shared, bytes(32)):
        raise AuthError("düşük dereceli açık anahtar")
    info = b"uzakmasa-v1|" + nid + b"|" + pub_c + pub_h + nonce_c + nonce_h
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=key, info=info).derive(shared)
    return okm[:32], okm[32:]


def _refuse(sock, why: str) -> AuthError:
    send_reject(sock, b"I", linger=0)  # hello okundu; bekletmek yalnızca slot işgal ettirir
    return AuthError(why, "I")


def client_handshake(sock, target_id: str, key: bytes) -> Channel:
    _prepare(sock, key)
    nid = norm_id(target_id).encode("utf-8")
    if not 0 < len(nid) <= _HELLO_MAX - _HELLO_FIXED:
        raise AuthError("geçersiz ID", "I")
    priv = X25519PrivateKey.generate()
    pub_c, nonce_c = priv.public_key().public_bytes_raw(), os.urandom(16)
    sock.sendall(MAGIC + pub_c + nonce_c + bytes([len(nid)]) + nid)
    head = _read_exact(sock, 4)
    if head == REJECT:
        raise _rejected(sock)
    reply = head + _read_exact(sock, 44)
    pub_h, nonce_h = reply[:32], reply[32:]
    c2h, h2c = _keys(priv, pub_h, key, nid, pub_c, pub_h, nonce_c, nonce_h)
    return Channel(sock, c2h, h2c)


def server_handshake(sock, my_id: str, key: bytes) -> Channel:
    _prepare(sock, key)
    nid = norm_id(my_id)
    if _read_exact(sock, 3) != MAGIC:
        raise _refuse(sock, "hatalı başlık")
    rest = _read_exact(sock, _HELLO_FIXED - 3)
    pub_c, nonce_c, idlen = rest[:32], rest[32:48], rest[48]
    if _HELLO_FIXED + idlen > _HELLO_MAX:
        raise _refuse(sock, "ID çok uzun")
    try:
        peer_id = norm_id(_read_exact(sock, idlen).decode("utf-8"))
    except UnicodeDecodeError:
        raise _refuse(sock, "ID çözülemedi") from None
    if peer_id != nid:
        raise _refuse(sock, "yanlış ID")
    priv = X25519PrivateKey.generate()
    pub_h, nonce_h = priv.public_key().public_bytes_raw(), os.urandom(16)
    try:
        c2h, h2c = _keys(priv, pub_c, key, nid.encode("utf-8"), pub_c, pub_h, nonce_c, nonce_h)
    except AuthError:  # bozuk hello: anahtarımızı göstermeden reddet
        raise _refuse(sock, "geçersiz açık anahtar") from None
    sock.sendall(pub_h + nonce_h)
    return Channel(sock, h2c, c2h)
