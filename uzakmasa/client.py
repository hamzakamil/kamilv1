"""İstemci motoru: Host'a bağlanır, kareleri çözer, girdiyi yollar. tkinter yok; ui çağrıları işçi iş parçacıklarından gelir."""
import collections
import io
import json
import socket
import threading
import time

from PIL import Image

from . import proto

# Süreler modül sabiti: testler kısaltabilsin (SPEC §4, §6 değerleri)
RETRIES = 20            # kurulu oturum kaybından sonra yeniden deneme hakkı
RETRY_DELAY = 3.0       # ilk deneme dahil: Host eski oturumu hâlâ tutuyorsa hemen vurmak "meşgul" yer
WAIT_ANSWER = 40        # W/D bekleme: Host onayı en çok 30 sn
IDLE_TIMEOUT = 10       # kurulu oturumda sessizlik sınırı (üç kaçan P)
KEEPALIVE = 3           # bu kadar sessizlikte P
MAX_PIXELS = 32_000_000  # Host'un kareleri ≤1280 px genişlikte; sahte dev boyutlu JPEG belleği şişirmesin

TEXTS = {
    "auth": "Şifre yanlış",
    "id": "ID yanlış",
    "busy": "Host meşgul",
    "denied": "Host bağlantıya izin vermedi",
    "kicked": "Host bağlantıyı kesti",
    "unreachable": "Host'a ulaşılamadı. ID'yi ve internet bağlantısını kontrol edin",
    "offline": "Host çevrimdışı görünüyor (relay'de kayıtlı değil)",
    "lost": "Bağlantı koptu",
    "stopped": "Bağlantı kapatıldı",
}
_ALWAYS = {"lost", "busy"}                  # ilk denemede de yeniden denenir
_AFTER_CONNECT = {"offline", "unreachable"}  # yalnız bir kez bağlanmış olduktan sonra; ilk denemede kesin hata
_REJECT_CODES = {"P": "auth", "I": "id", "B": "busy", "K": "kicked"}


class _Fail(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code, self.text = code, text


def _code(e):
    return _REJECT_CODES.get(e.reason, "lost") if isinstance(e, proto.AuthError) else "lost"


class Client:
    def __init__(self, conn_id, password, ui, uid, relay=None):
        self.conn_id, self.password, self.ui, self.uid, self.relay = conn_id, password, ui, uid, relay
        self._stop = threading.Event()
        self._thread = None
        self._sock = self._ch = None
        self._live = False  # oturum kurulu: girdi yalnız o zaman kabul edilir (bayat tıklar sonradan yeniden oynatılmasın)
        self._cv = threading.Condition()
        self._pending = collections.deque()  # (hareket_mi, json_baytı)
        self._last_tx = 0.0

    # --- dış yüzey: hiçbiri bloklamaz ---

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="client-main", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        ch, sock = self._ch, self._sock
        # shortcut: Host'a Q yollanmaz, kapanış Host'ta EOF/reset olarak görünür (Host Q'yu da anlar), yükseltme: yardımcı iş parçacığında Q + linger
        if ch is not None:
            ch.close()
        elif sock is not None:
            sock.close()

    def send_input(self, ev):
        if not self._live:
            return
        try:
            data = json.dumps(ev, separators=(",", ":")).encode("ascii")  # ASCII kaçışlı: tek başına vekil karakter de patlatmaz
        except (TypeError, ValueError):
            return
        move = isinstance(ev, dict) and ev.get("t") == "m"
        with self._cv:
            if move and self._pending and self._pending[-1][0]:
                self._pending[-1] = (True, data)  # art arda hareketlerde yalnız en yenisi: fare seli gecikme biriktirmez
            else:
                self._pending.append((move, data))
            self._cv.notify()

    # --- işçi ---

    def _run(self):
        try:
            self._main()
        except Exception as e:  # arayüz takılı kalmasın
            self.ui.closed("lost", "Beklenmeyen hata: %r" % e, True)

    def _target(self):
        """(normalleşmiş kimlik, (host, port)) doğrudan; (kimlik, None) relay. Anlaşılmazsa _Fail('input')."""
        tid = proto.norm_id(self.conn_id)
        if ":" in tid:
            host, _, port = tid.rpartition(":")
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise _Fail("input", "ID biçimi anlaşılmadı: 'sunucu:port' bekleniyor")
            return tid, (host, int(port))
        if tid.isascii() and tid.isdigit() and len(tid) <= 64:
            if not self.relay:
                raise _Fail("input", "Numaralı ID için relay adresi gerekli (Gelişmiş bölümü)")
            token = (self.relay[2] or "").strip()
            if "\n" in token or "\r" in token:
                raise _Fail("input", "Relay token'ı tek satır olmalı")
            return tid, None
        raise _Fail("input", "ID anlaşılmadı: ngrok adresi (sunucu:port) ya da relay numarası girin")

    def _main(self):
        try:
            tid, addr = self._target()
        except _Fail as f:
            self.ui.closed(f.code, f.text, True)
            return
        key = proto.derive_key(self.password)  # ~80 ms: Tk iş parçacığında değil, burada
        tries, up_before = 0, False
        while not self._stop.is_set():
            code, text, up = self._attempt(key, tid, addr)
            if self._stop.is_set():
                break
            up_before = up_before or up
            tries = 0 if up else tries  # kurulu oturum bütçeyi yeniler
            if not (code in _ALWAYS or (up_before and code in _AFTER_CONNECT)):
                self.ui.closed(code, text, True)
                return
            tries += 1
            if tries > RETRIES:
                self.ui.closed(code, "%s (deneme hakkı bitti)" % text, True)
                return
            self.ui.closed(code, "%s, tekrar deneniyor… (%d/%d)" % (text, tries, RETRIES), False)
            if self._stop.wait(RETRY_DELAY):
                break
        self.ui.closed("stopped", TEXTS["stopped"], True)

    def _dial(self, tid, addr):
        if addr is not None:
            try:
                self._sock = socket.create_connection(addr, 10)
            except OSError:
                raise _Fail("unreachable", TEXTS["unreachable"]) from None
            return self._sock
        host, port, token = self.relay
        try:
            self._sock = sock = socket.create_connection((host, port), 10)
        except OSError:
            raise _Fail("unreachable", "Relay sunucusuna ulaşılamadı") from None
        try:
            sock.settimeout(5)
            sock.sendall(("C %s %s\n" % (tid, (token or "").strip() or "-")).encode("utf-8"))
            reply = proto.read_line(sock)
        except (OSError, ValueError):
            sock.close()
            raise _Fail("unreachable", "Relay sunucusundan cevap alınamadı") from None
        if reply == "OK":
            return sock
        sock.close()
        if reply == "ERR offline":
            raise _Fail("offline", TEXTS["offline"])
        if reply == "ERR auth":
            raise _Fail("input", "Relay token'ı yanlış")
        if reply == "ERR format":
            raise _Fail("input", "Relay ID biçimi geçersiz")
        raise _Fail("unreachable", "Relay beklenmeyen cevap verdi")

    def _attempt(self, key, tid, addr):
        """Tek bağlantı denemesi ve oturumun tamamı: (kod, metin, oturum_kuruldu_mu)."""
        ch = sender = None
        done = threading.Event()
        up = False
        try:
            self.ui.status("Bağlanıyor…" if addr is not None else "Relay'e bağlanılıyor…")
            sock = self._dial(tid, addr)
            if self._stop.is_set():  # stop() soket atanmadan önce çalışmış olabilir
                raise _Fail("stopped", TEXTS["stopped"])
            self._ch = ch = proto.client_handshake(sock, tid, key)
            if self._stop.is_set():
                raise _Fail("stopped", TEXTS["stopped"])
            self._tx(ch, b"H", json.dumps({"uid": self.uid, "name": socket.gethostname()[:64], "v": 1}).encode())
            self.ui.status("Host onayı bekleniyor…")
            sock.settimeout(WAIT_ANSWER)
            try:
                kind, payload = ch.recv()
            except socket.timeout:  # Host sahibi 40 sn cevap vermedi: yeniden denemek onay penceresini yağmalar
                raise _Fail("denied", TEXTS["denied"] + " (zaman aşımı)") from None
            if kind == b"D":
                try:
                    why = json.loads(payload).get("why")
                except (ValueError, AttributeError):
                    why = None
                raise _Fail("denied", TEXTS["denied"] + (" (zaman aşımı)" if why == "timeout" else ""))
            if kind == b"Q":
                raise _Fail("kicked", TEXTS["kicked"])
            if kind != b"W":
                raise _Fail("lost", TEXTS["lost"])
            ch.trust()
            sock.settimeout(IDLE_TIMEOUT)
            up = True
            with self._cv:
                self._pending.clear()
            self._live = True
            sender = threading.Thread(target=self._sender, args=(ch, done), name="client-send", daemon=True)
            sender.start()
            self.ui.status("Bağlandı")
            self._receive(ch)
        except _Fail as f:
            return f.code, f.text, up
        except (proto.AuthError, OSError, ValueError) as e:
            code = _code(e)
            return code, TEXTS[code], up
        finally:
            self._live = False
            done.set()
            with self._cv:
                self._cv.notify_all()
            ch, sock = ch or self._ch, self._sock
            if ch is not None:
                ch.close()
            elif sock is not None:
                sock.close()
            self._ch = self._sock = None
            if sender is not None:
                sender.join(2)

    def _tx(self, ch, kind, payload=b""):
        ch.send(kind, payload)
        self._last_tx = time.monotonic()

    def _receive(self, ch):
        locked = False
        while True:
            kind, payload = ch.recv()
            if self._stop.is_set():
                raise _Fail("stopped", TEXTS["stopped"])
            if kind == b"F":
                if locked:  # kare geliyorsa Host kilitli değildir (sürekli değişen ekranda P hiç gelmez)
                    locked = False
                    self.ui.status("Bağlandı")
                self._show(payload)
                self._tx(ch, b"A")  # çözülemeyen karede de ack: Host penceresi tıkanmasın
            elif kind == b"P":
                if (payload == b"L") != locked:
                    locked = payload == b"L"
                    self.ui.status("Host ekranı kilitli / UAC açık" if locked else "Bağlandı")
            elif kind == b"Q":
                raise _Fail("kicked", TEXTS["kicked"])
            # bilinmeyen türler yok sayılır

    def _show(self, payload):
        try:
            img = Image.open(io.BytesIO(payload), formats=["JPEG"])  # yalnız JPEG çözücü: saldırı yüzeyi küçük
            if img.width * img.height > MAX_PIXELS:
                return
            img.load()
            if img.mode != "RGB":
                img = img.convert("RGB")
        except Exception:  # bozuk kare atlanır; oturum bundan düşmez
            return
        self.ui.frame(img)

    def _sender(self, ch, done):
        try:
            while not done.is_set():
                with self._cv:
                    if not self._pending:
                        self._cv.wait(max(0.01, KEEPALIVE - (time.monotonic() - self._last_tx)))
                    batch = list(self._pending)
                    self._pending.clear()
                for _, data in batch:
                    self._tx(ch, b"I", data)
                if time.monotonic() - self._last_tx >= KEEPALIVE:
                    self._tx(ch, b"P")
        except OSError:
            pass  # kanal kapandı ya da gönderim koptu: oturum bitiyor
        finally:
            done.set()
            ch.close()  # gönderici ölürse alıcı da uyansın
