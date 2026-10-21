"""Host motoru: ekranı akıtır, uzaktan gelen girdiyi uygular. tkinter yok; ui'ye yalnız işçi iş parçacıklarından dokunulur."""
import collections
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import traceback
from types import SimpleNamespace

from pyngrok import ngrok
from pyngrok.conf import PyngrokConfig

from . import config, proto, winio

# Süreler modül sabiti: testler kısaltabilsin (SPEC §4-5 değerleri)
MAX_PRE_AUTH = 8        # eşzamanlı doğrulanmamış bağlantı
CONSENT_TIMEOUT = 30    # sahibin "izin ver" demesi için
IDLE_TIMEOUT = 10       # kurulu oturumda sessizlik sınırı (üç kaçan P)
KEEPALIVE = 3           # bu kadar sessizlikte P
IDLE_POLL = 0.25        # ekran değişmediyse yoklama aralığı
BURST = 0.5             # girdiden sonra bu süre tam hızda yokla: tuş yankısı bir sonraki 0.25 sn'ye kalmasın
ACK_STALL = 2.0         # ack hiç gelmezse pencere sıfırlanır
STATS_EVERY = 2.0
RELAY_WAIT = 60         # relay'den K gelmezse kayıt ölü sayılır
RELAY_HOLD_MIN = 5      # bundan kısa yaşayan kayıt sağlıklı sayılmaz: geri çekilme uygulanır
RETRY_MIN, RETRY_MAX = 2, 30
BORE_CONTROL = 7835     # bore sunucusunun kontrol portu (ekzhang/bore protokolü)
BORE_FRAME_MAX = 256    # bore çerçevesi: JSON + "\0", en çok bu kadar bayt
MAX_BORE_ACCEPT = 16    # eşzamanlı Accept bağlantısı: ziyaretçi seli iş parçacığı seli olmasın

_UID = re.compile(r"[0-9a-f]{32}")


def _addr_text(addr):
    return "%s:%s" % tuple(addr[:2]) if isinstance(addr, tuple) else str(addr)


def _clean_name(v):
    s = "".join(c for c in v if c.isprintable()).strip() if isinstance(v, str) else ""
    return s[:64].strip() or "Bilinmeyen"


def _parse_hello(payload):
    """(uid, ad) ya da geçersizse None."""
    try:
        info = json.loads(payload)
        uid, name = info["uid"], info.get("name")
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    if type(uid) is not str or not _UID.fullmatch(uid):
        return None
    return uid, _clean_name(name)


def _pos(v):
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError("koordinat sayı olmalı")
    return float(v)


def _keepalive(sock):
    # sessizce ölen NAT eşlemesi 2 saat yerine ~1 dk'da fark edilsin (yalnız Windows; yoksa yok say)
    try:
        sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 10_000))
    except (AttributeError, OSError, ValueError):
        pass


def _bore_send(sock, msg):
    sock.sendall(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\0")


def _bore_frame(sock, buf):
    """Bir bore çerçevesi okur (JSON, sonu "\\0"). Kontrol soketinde ham veri yoktur, bu yüzden arabellek güvenli."""
    while b"\0" not in buf:
        if len(buf) > BORE_FRAME_MAX:
            raise ValueError("bore çerçevesi çok uzun")
        chunk = sock.recv(BORE_FRAME_MAX)
        if not chunk:
            raise ConnectionError("bore sunucusu bağlantıyı kapattı")
        buf += chunk
    frame, _, rest = bytes(buf).partition(b"\0")
    buf[:] = rest
    return json.loads(frame)


def _ngrok_hint(e, token):
    msg = " ".join(str(e).split())
    if token:
        msg = msg.replace(token, "***")  # hata metni asla kimlik bilgisi taşımasın
    low = msg.lower()
    # sıra önemli: 108 ve 8013 mesajları da "authentication failed" ile başlar
    if "err_ngrok_108" in low or "simultaneous" in low or "already" in low:
        hint = "Başka bir ngrok oturumu açık olabilir: Görev Yöneticisi'nden ngrok.exe'yi kapatın"
    elif "err_ngrok_8013" in low or "card" in low:
        hint = "Ücretsiz ngrok hesabında TCP için kart doğrulaması gerekir (yalnız doğrulama)"
    elif "authtoken" in low or "err_ngrok_105" in low or "authentication" in low:
        hint = "ngrok Authtoken eksik ya da geçersiz: ngrok.com panelinden kopyalayın"
    elif any(w in low for w in ("urlerror", "getaddrinfo", "timed out", "download", "connection", "network")):
        hint = "İnternet bağlantısı yok ya da ngrok indirilemedi (engellenmiş olabilir)"
    else:
        hint = "ngrok başlatılamadı"
    return "%s. (%s)" % (hint, msg[:200])


class Host:
    def __init__(self, cfg, ui, mode, save=lambda cfg: None, grabber_factory=None, injector=None, fail_delay=1.0):
        if mode not in ("bore", "ngrok", "relay"):
            raise ValueError("kip 'bore', 'ngrok' ya da 'relay' olmalı")
        self._key = bytes.fromhex(cfg.get("k", ""))
        if len(self._key) != 32:
            raise ValueError("şifre anahtarı (k) 32 bayt olmalı")
        self.cfg, self.ui, self.mode, self._save = cfg, ui, mode, save
        self._grabber_factory = grabber_factory or winio.Grabber  # mss örneği iş parçacığına bağlı: akıtıcıda çağrılır
        self.injector = injector or winio.Injector()
        self.fail_delay = fail_delay
        self.conn_id = cfg.get("relay_id", "") if mode == "relay" else ""
        cfg.setdefault("trusted", [])
        self._stopped = threading.Event()
        self._started = False
        self._session = threading.Lock()  # tek oturum yuvası
        self._pre = threading.BoundedSemaphore(MAX_PRE_AUTH)
        self._cfg_lock = threading.Lock()
        self._warned = {}
        self._sent = 0  # biten oturumların toplam gönderimi
        self._ch = None  # etkin oturumun kanalı ve istemci özeti: kick/stop buradan kapatır
        self._uid_hash = None
        self._listener = None
        self._relay_sock = None  # relay ya da bore kontrol soketi: stop() bunu kapatıp bekleyen okumayı uyandırır
        self._bore_slots = threading.BoundedSemaphore(MAX_BORE_ACCEPT)
        self._ncfg = None

    # --- dış yüzey: hiçbiri bloklamaz, hiçbiri ui çağırmaz (çağıran Tk iş parçacığı olabilir) ---

    def start(self):
        if self._started:
            return
        self._started = True
        run = {"bore": self._run_bore, "ngrok": self._run_ngrok, "relay": self._run_relay}[self.mode]
        threading.Thread(target=self._guard, args=(run,), name="host-main", daemon=True).start()

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()  # bekleyen onay da bununla reddedilir (_ask_consent)
        threading.Thread(target=self._guard, args=(self._shutdown, self._ch), name="host-stop", daemon=True).start()

    def kick(self):
        ch, h = self._ch, self._uid_hash
        if ch is None:
            return

        def drop():
            if h:  # bir sonraki deneme yeniden onay istesin
                with self._cfg_lock:
                    while h in self.cfg["trusted"]:
                        self.cfg["trusted"].remove(h)
                self._persist()
            self._bye(ch)

        threading.Thread(target=self._guard, args=(drop,), name="host-kick", daemon=True).start()

    def _shutdown(self, ch):
        for s in (self._listener, self._relay_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if ch is not None:
            self._bye(ch)
        if self._ncfg is not None:
            self._kill_ngrok()  # pyngrok kilidi connect tarafından tutuluyorsa burada bekler: bu iş parçacığı bunun için

    def _bye(self, ch):
        try:
            ch.send(b"Q", lock_timeout=0.5)  # akıtıcı kilidi tutuyorsa 0.5 sn sonra vazgeç
        except Exception:
            pass
        ch.close(linger=0.5)  # linger: Q'nun, istemcinin A'larına karşı RST ile yutulmaması için

    def _kill_ngrok(self):
        try:
            ngrok.kill(self._ncfg)
        except Exception as e:
            self._log("ngrok kapatılamadı: %r" % e)

    # --- yardımcılar ---

    def _log(self, text):
        try:
            self.ui.log(text)
        except Exception:
            pass

    def _warn(self, key, text):
        # saldırgan ya da bozuk eş log'u boğmasın: anahtar başına 10 sn'de en çok bir satır
        now = time.monotonic()
        if now - self._warned.get(key, -1e9) >= 10:
            self._warned[key] = now
            self._log(text)

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            self._log("Beklenmeyen hata:\n" + traceback.format_exc())

    def _persist(self):
        try:
            self._save(self.cfg)
        except Exception as e:
            self._log("Ayarlar kaydedilemedi: %r" % e)

    # --- ngrok kipi ---

    def _run_ngrok(self):
        self._log("ngrok hazırlanıyor (ilk çalıştırmada indirilir)…")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(16)
            srv.settimeout(0.5)  # close() accept'i uyandırmasa bile döngü ≤0.5 sn'de çıkar
            self._listener = srv
            token = self.cfg.get("ngrok_token") or config.DEFAULT_NGROK_TOKEN or None  # None: pyngrok NGROK_AUTHTOKEN'a bakar
            # özel yol: kill() yola göre çalışır, kullanıcının kendi ngrok'una asla dokunmaz
            ncfg = PyngrokConfig(ngrok_path=os.path.join(config.APPDIR, "ngrok.exe"),
                                 config_path=os.path.join(config.APPDIR, "ngrok.yml"),
                                 auth_token=token, startup_timeout=30)
            self._ncfg = ncfg
            if self._stopped.is_set():
                return
            try:
                ngrok.kill(ncfg)  # çöken önceki çalışmadan kalan ngrok.exe
                # pyngrok 9: "127.0.0.1:port" biçimi "localhost:127.0.0.1:port" olur (bozuk); "tcp://" öneki olduğu gibi geçer
                tunnel = ngrok.connect(addr="tcp://127.0.0.1:%d" % srv.getsockname()[1], proto="tcp", pyngrok_config=ncfg)
            except Exception as e:
                if not self._stopped.is_set():
                    self.ui.error(_ngrok_hint(e, token))
                self._kill_ngrok()  # yarım kalan ngrok.exe bir sonraki Başlat'ı "başka oturum" diye bozmasın
                return
            if self._stopped.is_set():  # connect sürerken Durdur'a basılmış: geç gelen tüneli kapat
                self._kill_ngrok()
                return
            self.conn_id = tunnel.public_url.split("://", 1)[1]
            self.ui.ready(self.conn_id)
            self._accept_loop(srv)
        finally:
            srv.close()

    def _accept_loop(self, srv):
        winio.keep_awake(True)  # SetThreadExecutionState iş parçacığına bağlı: açan ve kapatan bu uzun ömürlü iş parçacığı
        self._log("Uyku engellendi")
        try:
            while not self._stopped.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    if self._stopped.is_set():
                        break
                    self._warn("accept", "Bağlantı kabul edilemedi: %s" % e)
                    time.sleep(0.1)
                    continue
                threading.Thread(target=self._guard, args=(self.serve_conn, conn, addr), name="host-conn", daemon=True).start()
        finally:
            winio.keep_awake(False)

    # --- bore kipi: hesapsız, ücretsiz herkese açık tünel (ekzhang/bore protokolü; sunucu varsayılanı bore.pub) ---

    def _run_bore(self):
        host, _, cp = (self.cfg.get("bore_server") or config.DEFAULT_BORE_SERVER).strip().partition(":")
        if not host or (cp and not (cp.isascii() and cp.isdigit() and 0 < int(cp) < 65536)):
            self.ui.error("bore sunucusu 'sunucu' ya da 'sunucu:port' biçiminde olmalı")
            return
        control = (host, int(cp) if cp else BORE_CONTROL)
        want = self.cfg.get("bore_port")
        want = want if type(want) is int and 1024 <= want <= 65535 else 0  # önceki port: ID kararlı kalsın
        backoff, awake = RETRY_MIN, False
        try:
            while not self._stopped.is_set():
                ctrl = held = None
                try:
                    ctrl = socket.create_connection(control, 10)
                    self._relay_sock = ctrl
                    if self._stopped.is_set():
                        return
                    _keepalive(ctrl)
                    ctrl.settimeout(10)  # sunucu ~0.5 sn'de bir Heartbeat yollar: 10 sn sessizlik = ölü
                    buf = bytearray()
                    _bore_send(ctrl, {"Hello": want})
                    msg = _bore_frame(ctrl, buf)
                    if want and isinstance(msg, dict) and "Error" in msg:
                        want = 0  # eski port dolu ya da aralık dışı: rastgele port iste
                        self._log("Önceki genel port alınamadı, yeni port isteniyor")
                        continue
                    if not (isinstance(msg, dict) and type(msg.get("Hello")) is int):
                        if isinstance(msg, dict) and "Challenge" in msg:
                            self.ui.error("Bu bore sunucusu gizli anahtar istiyor: bore.pub'u kullanın ya da relay kipini seçin")
                            return
                        raise ConnectionError("bore cevabı: %.80r" % (msg,))
                    want = msg["Hello"]
                    with self._cfg_lock:
                        self.cfg["bore_port"] = want
                    self._persist()
                    self.conn_id = "%s:%d" % (host, want)
                    held = time.monotonic()
                    self.ui.ready(self.conn_id)
                    if not awake:
                        winio.keep_awake(True)
                        awake = True
                        self._log("Uyku engellendi")
                    while not self._stopped.is_set():
                        msg = _bore_frame(ctrl, buf)
                        uid = msg.get("Connection") if isinstance(msg, dict) else None  # "Heartbeat" düz metindir: yok say
                        if type(uid) is str and len(uid) <= 64 and self._bore_slots.acquire(blocking=False):
                            threading.Thread(target=self._guard, args=(self._bore_accept, control, uid),
                                             name="host-conn", daemon=True).start()
                except Exception as e:  # ağ hatası da beklenmeyen hata da döngüyü öldürmesin
                    if not self._stopped.is_set():
                        kind = "" if isinstance(e, (OSError, ValueError)) else "\n" + traceback.format_exc()
                        self._log("bore bağlantısı kesildi: %r%s" % (e, kind))
                finally:
                    if ctrl is not None:
                        ctrl.close()
                if self._stopped.is_set():
                    break
                if held is not None and time.monotonic() - held >= RELAY_HOLD_MIN:
                    backoff, delay = RETRY_MIN, 0  # sağlıklı bağlantı kaybı: hemen yeniden bağlan
                else:
                    delay, backoff = backoff, min(backoff * 2, RETRY_MAX)
                self._stopped.wait(delay)
        finally:
            if awake:
                winio.keep_awake(False)

    def _bore_accept(self, control, uid):
        """Sunucu "Connection" deyince: yeni bağlantı aç, Accept yolla; sonrası ziyaretçiye giden ham akıştır."""
        try:
            sock = socket.create_connection(control, 10)
            try:
                _bore_send(sock, {"Accept": uid})
            except OSError:
                sock.close()
                raise
            self.serve_conn(sock, "bore tüneli")  # kapatmayı serve_conn yapar
        finally:
            self._bore_slots.release()

    # --- relay kipi ---

    def _run_relay(self):
        host, _, port = self.cfg.get("relay", "").strip().rpartition(":")
        token = (self.cfg.get("relay_token") or "").strip() or "-"
        if not host or not port.isdigit() or not 0 < int(port) < 65536 or "\n" in token or "\r" in token:
            self.ui.error("Relay adresi 'sunucu:port' biçiminde, token tek satır olmalı")
            return
        addr = (host, int(port))
        preface = ("H %s %s\n" % (self.conn_id, token)).encode("utf-8")
        backoff, awake = RETRY_MIN, False
        try:
            while not self._stopped.is_set():
                sock = held = None
                served = False
                try:
                    sock = socket.create_connection(addr, 10)
                    self._relay_sock = sock
                    if self._stopped.is_set():
                        return
                    _keepalive(sock)
                    sock.settimeout(10)
                    sock.sendall(preface)
                    reply = proto.read_line(sock)
                    if reply in ("ERR auth", "ERR format"):  # ayar hatası: yeniden denemek işe yaramaz
                        self.ui.error("Relay token'ı yanlış" if reply == "ERR auth" else "Relay ID/token biçimi geçersiz")
                        return
                    if reply != "OK":
                        raise ConnectionError("relay cevabı: %s" % reply[:40])
                    held = time.monotonic()
                    self.ui.ready(self.conn_id)
                    if not awake:
                        winio.keep_awake(True)
                        awake = True
                        self._log("Uyku engellendi")
                    sock.settimeout(RELAY_WAIT)  # K (20 sn'de bir) gelmezse kayıt ölü: yeniden kaydol
                    while proto.read_line(sock) != "GO":
                        pass
                    # shortcut: oturum sürerken relay'de kayıtlı değiliz (ikinci istemci "offline" görür), yükseltme: serve_conn'u iş parçacığında çalıştırıp hemen yeniden kaydol
                    self.serve_conn(sock, addr)
                    served = True
                except Exception as e:  # ağ hatası da, serve_conn'daki beklenmeyen hata da döngüyü öldürmesin
                    if not self._stopped.is_set():
                        kind = "" if isinstance(e, (OSError, ValueError)) else "\n" + traceback.format_exc()
                        self._log("Relay bağlantısı kesildi: %r%s" % (e, kind))
                finally:
                    if sock is not None:
                        sock.close()
                if self._stopped.is_set():
                    break
                if served or (held is not None and time.monotonic() - held >= RELAY_HOLD_MIN):
                    backoff, delay = RETRY_MIN, 0  # sağlıklı kayıt kaybı: hemen yeniden kaydol
                else:
                    delay, backoff = backoff, min(backoff * 2, RETRY_MAX)  # aynı kimlikli ikinci Host kaydı kapıştırmasın
                self._stopped.wait(delay)
        finally:
            if awake:
                winio.keep_awake(False)

    # --- bir bağlantının ömrü ---

    def serve_conn(self, conn, addr):
        st = SimpleNamespace(ch=None, pre=False, owner=False, up=False, s=None, streamer=None)
        try:
            self._serve(conn, _addr_text(addr), st)
        except (OSError, ValueError, proto.AuthError) as e:
            if st.up:
                self._log("Bağlantı sona erdi: %s" % e)
        finally:
            self._cleanup(conn, st)

    def _serve(self, conn, addr, st):
        # (1) etkin oturum ya da durdurma: yalnızca bak, yuvayı henüz alma
        if self._session.locked() or self._stopped.is_set():
            proto.send_reject(conn, b"B")
            return
        # (2) doğrulanmamış bağlantı sınırı
        if not self._pre.acquire(blocking=False):
            return
        st.pre = True
        # (3) el sıkışma: kötü başlık/ID için server_handshake REJECT I'yı zaten yolladı; sayılmaz, tarpit yok
        try:
            st.ch = ch = proto.server_handshake(conn, self.conn_id, self._key)
        except (proto.AuthError, OSError, ValueError):
            return
        # (4) ilk çerçeve çözülemezse yanlış şifre
        try:
            kind, payload = ch.recv()
        except proto.AuthError:
            self._warn("auth", "Yanlış şifreyle bağlantı denemesi: %s" % addr)
            time.sleep(self.fail_delay)  # tarpit: yalnız bu ön-doğrulama yuvasını tutar
            proto.send_reject(conn, b"P")
            return
        except (OSError, ValueError):
            return
        # (5) tek oturum yuvası şifre doğrulandıktan sonra alınır
        if not self._session.acquire(blocking=False):
            proto.send_reject(conn, b"B")
            return
        st.owner = True
        self._pre.release()
        st.pre = False
        ch.trust()
        self._ch = ch
        if self._stopped.is_set():  # stop() kanalı göremeden yayımlanmış olabilir
            return
        # (6) H doğrula, güven/onay
        hello = _parse_hello(payload) if kind == b"H" else None
        if hello is None:
            proto.send_reject(conn, b"I", linger=0)
            return
        uid, name = hello
        h = hashlib.sha256(uid.encode()).hexdigest()
        self._uid_hash = h
        with self._cfg_lock:
            known = h in self.cfg["trusted"]
        if not known:
            why = self._ask_consent(name, addr)
            if why:
                ch.send(b"D", json.dumps({"why": why}).encode())
                ch.close(linger=0.5)  # D'nin RST ile yutulmaması için
                return
            with self._cfg_lock:
                if h not in self.cfg["trusted"]:
                    self.cfg["trusted"].append(h)
            self._persist()
        if self._stopped.is_set():
            return
        # (7) kabul
        ch.send(b"W", json.dumps({"name": socket.gethostname()[:64], "v": 1}).encode())
        conn.settimeout(IDLE_TIMEOUT)
        st.up = True
        self.ui.connected(name, addr)
        # (8) akıtıcı + okuyucu
        st.s = s = SimpleNamespace(sent=collections.deque(), q=55, wake=threading.Event(), done=threading.Event())
        st.streamer = threading.Thread(target=self._guard, args=(self._stream, ch, s), name="host-stream", daemon=True)
        st.streamer.start()
        self._read(ch, s)

    def _cleanup(self, conn, st):
        try:
            if st.s is not None:
                st.s.done.set()
                st.s.wake.set()
            (st.ch or conn).close()  # önce kanal: Windows'ta bloklu send/recv'i yalnız close() uyandırır
            if st.streamer is not None:
                st.streamer.join(5)
            if st.up:
                self.injector.release_all()  # takılı kalmış Ctrl/Shift/fare düğmesi Host sahibini bozmasın
        finally:
            try:
                if st.owner:
                    self._sent += st.ch.bytes_sent
                if st.up:
                    self.ui.stats(self._sent)
                    self.ui.disconnected()
            finally:
                if st.pre:
                    self._pre.release()
                if st.owner:
                    self._ch = self._uid_hash = None
                    self._session.release()

    def _ask_consent(self, name, addr):
        """None: izin verildi; yoksa D sebebi ("denied" | "timeout")."""
        box, done = [], threading.Event()

        def ask():  # ui.consent iptal edilemez; stop() beklemeyi buradan keser, pencere kendi süresinde kapanır
            try:
                box.append(bool(self.ui.consent(name, addr)))
            except Exception as e:
                self._log("Onay penceresi hatası: %r" % e)
            finally:
                done.set()

        t0 = time.monotonic()
        threading.Thread(target=ask, name="host-consent", daemon=True).start()
        while not done.wait(0.1) and not self._stopped.is_set() and time.monotonic() - t0 < CONSENT_TIMEOUT:
            pass
        if self._stopped.is_set():
            return "denied"
        if box and box[0]:
            return None
        # pencerenin kendi geri sayımı da süre dolunca False döner: süreye yakınsa zaman aşımıdır
        return "timeout" if time.monotonic() - t0 >= CONSENT_TIMEOUT * 0.9 else "denied"

    def _read(self, ch, s):
        # istisna (EOF, zaman aşımı, etiket hatası) oturumu bitirir; zaman aşımında asla yeniden okuma yok
        while True:
            kind, payload = ch.recv()
            if kind == b"I":
                self._inject(payload)
                s.wake.set()
            elif kind == b"A":
                try:
                    t0 = s.sent.popleft()
                except IndexError:  # pencere sıfırlanmıştı
                    continue
                rtt = time.monotonic() - t0
                if rtt > 0.25:
                    s.q = max(30, s.q - 10)
                elif rtt < 0.10:
                    s.q = min(70, s.q + 3)
            elif kind == b"Q":
                return
            # P ve bilinmeyenler yok sayılır

    def _inject(self, payload):
        if len(payload) > 512:  # olaylar küçüktür
            return
        inj = self.injector
        try:
            ev = json.loads(payload)
            t = ev["t"]
            if t == "m":
                inj.move(_pos(ev["x"]), _pos(ev["y"]))  # [0,1] dışını Injector sıkıştırır
            elif t == "b":
                b, d = ev["b"], ev["d"]
                if b not in ("l", "m", "r") or type(d) is not int or d not in (0, 1):
                    raise ValueError("düğme olayı geçersiz")
                xy = (_pos(ev["x"]), _pos(ev["y"])) if "x" in ev or "y" in ev else (None, None)
                inj.button(b, d == 1, *xy)
            elif t == "w":
                d = ev["d"]
                if type(d) is not int or d == 0 or not -1200 <= d <= 1200:
                    raise ValueError("tekerlek değeri geçersiz")
                inj.wheel(d)
            elif t == "c":
                c = ev["c"]
                if type(c) is not str or len(c) != 1:
                    raise ValueError("tek karakter bekleniyor")
                inj.char(c)
            elif t == "k":
                vk, d = ev["vk"], ev["d"]
                if type(vk) is not int or not 1 <= vk <= 254 or type(d) is not int or d not in (0, 1):
                    raise ValueError("tuş olayı geçersiz")
                inj.key(vk, d == 1)
            else:
                raise ValueError("bilinmeyen olay türü")
        except Exception as e:  # bozuk olay ya da SendInput engeli (UIPI, kilit) oturumu düşürmez; içerik asla loglanmaz
            self._warn("input", "Girdi olayı uygulanamadı: %s" % e)

    # --- akıtıcı ---

    def _stream(self, ch, s):
        # shortcut: yerleşme karesi yok (durağan ekran son uyarlanmış kalitede kalır), sabit 1280 px genişlik, yükseltme: 0.5 sn durağanlıktan sonra q=70 kare
        gap = 1 / (8 if self.mode == "ngrok" else 12)
        grabber = prev = None
        locked = False
        now = time.monotonic()
        last_send = last_stats = now
        last_grab = burst_until = 0.0
        try:
            while not s.done.is_set():
                now = time.monotonic()
                was, locked = locked, winio.screen_locked()
                if locked != was:  # yalnız durum değişince logla
                    self._log("Ekran kilitli ya da UAC açık: görüntü durdu" if locked else "Ekran artık kilitli değil: görüntü sürüyor")
                if now - last_send >= KEEPALIVE:
                    ch.send(b"P", b"L" if locked else b"")
                    last_send = time.monotonic()
                if now - last_stats >= STATS_EVERY:
                    self.ui.stats(self._sent + ch.bytes_sent)
                    last_stats = now
                if locked:
                    s.done.wait(0.5)  # güvenli masaüstünde yakalama boşuna
                    continue
                if len(s.sent) >= 2:  # ack penceresi dolu: yakalama/kodlama yok
                    try:
                        if now - s.sent[0] > ACK_STALL:  # istemci hiç ack vermiyor: kilitlenme olmasın
                            s.sent.clear()
                    except IndexError:  # iki ack araya girdi, pencere zaten açıldı
                        pass
                    time.sleep(0.004)
                    continue
                if now - last_grab < gap:  # yakalama (ve dolayısıyla kare) aralığı ≥ 1/max_fps
                    time.sleep(last_grab + gap - now)
                last_grab = time.monotonic()
                try:
                    if grabber is None:
                        grabber = self._grabber_factory()
                    img, key = grabber.grab()
                except Exception as e:
                    self._warn("grab", "Ekran yakalanamadı: %r" % e)
                    self._close_grabber(grabber)
                    grabber = None  # bozuk örnek yerine bir sonraki turda yenisi kurulur
                    s.done.wait(0.5)
                    continue
                if key == prev:  # değişmedi: kare yok; girdi gelirse hemen uyan
                    if s.wake.wait(gap if time.monotonic() < burst_until else IDLE_POLL):
                        s.wake.clear()
                        burst_until = time.monotonic() + BURST
                    continue
                jpeg = winio.to_jpeg(img, s.q, 1280)
                s.sent.append(time.monotonic())  # önce say: ack, send dönmeden gelebilir
                ch.send(b"F", jpeg)
                prev = key
                last_send = time.monotonic()
        except OSError as e:  # kanal kapandı ya da gönderim koptu
            if not s.done.is_set():
                self._log("Görüntü gönderimi koptu: %s" % e)
        finally:
            s.done.set()
            ch.close()  # akıtıcı ölürse okuyucu da uyansın, oturum bitsin
            self._close_grabber(grabber)

    @staticmethod
    def _close_grabber(g):
        close = getattr(g, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
