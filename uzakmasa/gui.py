"""Tkinter arayüzü: açılış ekranı, Host paneli, Client paneli, görüntüleyici ve onay penceresi.

İş parçacığı kuralı: motor iş parçacıkları Tk'ye ASLA dokunmaz. Tk'ye yapılacak her iş App.post() ile kuyruğa
kapanış olarak bırakılır; Tk iş parçacığı kuyruğu ~30 ms'de bir boşaltır.
"""
import os
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageTk

from . import config, proto, winio
from .keymap import KeyMapper

TITLE = "Kamil V1"
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 31 sembol (0/O ve 1/I/L karışmaz): 12 sembol ≈ 59 bit
PW_LEN, MIN_CUSTOM_PW = 12, 16
SAVED_PW = "(kayıtlı şifre)"
ID_PLACEHOLDER = "(Başlat'a basınca oluşur)"
ID_PREPARING = "(hazırlanıyor…)"
NGROK_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"
WARN_BYTES = 800 * 1024 * 1024  # ngrok ücretsiz kotası 1 GB/ay
CONSENT_SECONDS = 30
CLOSE_WAIT = 5.0  # kapanışta motor kapatma iş parçacıklarını en çok bu kadar bekle
POLL_MS, VIEW_MS = 30, 16
ELAPSED_MS = 1000  # geçen bağlantı süresi etiketi saniyede bir yenilenir
MAX_UPSCALE = 1.5
BOLD = ("Segoe UI", 11, "bold")
TONES = {"": "TLabel", "hint": "Hint.TLabel", "good": "Good.TLabel", "bad": "Bad.TLabel", "warn": "Warn.TLabel"}


# --- saf yardımcılar (testlenebilir) ---

def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PW_LEN))


def parse_relay(text: str):
    """'host:port' -> (host, port) ya da None."""
    host, _, port = text.strip().rpartition(":")
    return (host, int(port)) if host and port.isascii() and port.isdigit() and 0 < int(port) < 65536 else None


def fmt_id(conn_id: str) -> str:
    digits = conn_id.replace(" ", "")
    return f"{digits[:3]} {digits[3:6]} {digits[6:]}" if len(digits) == 9 and digits.isdigit() else conn_id


def fmt_bytes(n: int) -> str:
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def fmt_duration(seconds: float) -> str:
    """Geçen süre: 1 saatten kısaysa D:SS, değilse S:DD:SS."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fit_rect(fw, fh, cw, ch):
    """Kareyi tuvale en-boy oranını koruyarak sığdırır; küçültme serbest, büyütme en çok MAX_UPSCALE.
    Dönüş: (çizim genişliği, çizim yüksekliği, sol, üst) tuval pikseli."""
    s = min(cw / fw, ch / fh, MAX_UPSCALE)
    dw, dh = min(cw, max(1, round(fw * s))), min(ch, max(1, round(fh * s)))
    return dw, dh, (cw - dw) // 2, (ch - dh) // 2


# shortcut: log.txt büyüklük sınırı yok, yükseltme: döndürmeli kayıt
def _log(text: str) -> None:
    """APPDIR\\log.txt'ye utf-8 ekler; kayıt hatası asla uygulamayı düşürmez."""
    try:
        os.makedirs(config.APPDIR, exist_ok=True)
        with open(os.path.join(config.APPDIR, "log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
    except Exception:
        pass


def _fix_streams() -> None:
    """--noconsole exe'de std akımlar None: pyngrok'un indirme ilerlemesi AttributeError verirdi."""
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w", encoding="utf-8"))


def _install_hooks(root) -> None:
    def fmt(et, ev, tb):
        return "".join(traceback.format_exception(et, ev, tb))

    sys.excepthook = lambda et, ev, tb: _log(fmt(et, ev, tb))
    threading.excepthook = lambda a: _log(fmt(a.exc_type, a.exc_value, a.exc_traceback))
    root.report_callback_exception = lambda et, ev, tb: _log(fmt(et, ev, tb))  # Tk geri çağrıları excepthook'a uğramaz


def _setup_style(root, px) -> None:
    for name in ("TkDefaultFont", "TkTextFont"):
        tkfont.nametofont(name).configure(family="Segoe UI", size=10)
    st = ttk.Style(root)
    st.configure("Head.TLabel", font=("Segoe UI", 15, "bold"))
    st.configure("Hint.TLabel", foreground="#5f6368")
    st.configure("Bad.TLabel", foreground="#b3261e")
    st.configure("Warn.TLabel", foreground="#9a5b00")
    st.configure("Good.TLabel", foreground="#1b7f3b")
    st.configure("Big.TButton", font=("Segoe UI", 12), padding=(px(16), px(14)))


def _tone(label, tone: str) -> None:
    label.configure(style=TONES[tone])


# --- motor iş parçacıkları <-> Tk köprüsü ---

class _Bridge:
    """Motorun ui nesnesi: çağrılar iş parçacığından gelir, Tk'ye kuyrukla geçer. dead: panel o motoru bıraktı."""
    dead = False

    def __init__(self, app, panel):
        self.app, self.panel = app, panel

    def _call(self, name, *args):
        def run():
            if not self.dead:
                getattr(self.panel, name)(*args)
        self.app.post(run)


class _Consent:
    """Tek bir bağlantı isteği. show/decide yalnızca Tk iş parçacığında çalışır; motor iş parçacığı ev'i bekler.
    Motor iş parçacığı bu nesneye son referansı bırakabilir: içinde tk.Variable/PhotoImage tutma (__del__ Tk'ye dokunur)."""

    def __init__(self):
        self.ev = threading.Event()
        self.result = self.done = False
        self.dlg = self.timer = None
        self.left = CONSENT_SECONDS

    def show(self, app, name, addr):
        if self.done:
            return
        root, P = app.root, app.px
        if root.state() == "iconic":
            root.deiconify()
        d = self.dlg = tk.Toplevel(root)  # transient YOK: ana pencere simge durumundayken transient pencere görünmez kalır
        d.title("Bağlantı isteği")
        d.resizable(False, False)
        d.attributes("-topmost", True)
        d.protocol("WM_DELETE_WINDOW", lambda: self.decide(False))
        d.bind("<Escape>", lambda e: self.decide(False))
        body = ttk.Frame(d, padding=P(16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"{name} ({addr}) bağlanmak istiyor", font=BOLD, wraplength=P(400)).pack(anchor="w")
        ttk.Label(body, style="Hint.TLabel", wraplength=P(400), text=(
            "İzin verirseniz bu cihaz hatırlanır ve sonraki bağlantıları sormadan kabul edilir. "
            "Bağlantıyı siz beklemiyorsanız Reddet'e basın.")).pack(anchor="w", pady=(P(8), P(4)))
        self.count = ttk.Label(body, style="Warn.TLabel")
        self.count.pack(anchor="w", pady=(0, P(12)))
        row = ttk.Frame(body)
        row.pack(anchor="e")
        self.allow_btn = ttk.Button(row, text="İzin ver ve hatırla", command=lambda: self.decide(True))
        self.allow_btn.pack(side="left", padx=(0, P(8)))
        self.deny_btn = ttk.Button(row, text="Reddet", command=lambda: self.decide(False))
        self.deny_btn.pack(side="left")
        d.update_idletasks()
        sw, sh = d.winfo_screenwidth(), d.winfo_screenheight()
        d.geometry(f"+{(sw - d.winfo_reqwidth()) // 2}+{(sh - d.winfo_reqheight()) // 3}")
        d.lift()
        d.bell()
        d.focus_force()
        self.deny_btn.focus_set()  # yanlışlıkla Boşluk/Enter reddeder, izin vermez
        self._tick()

    def _tick(self):
        if self.done:
            return
        if self.left <= 0:
            self.decide(False)
            return
        self.count.configure(text=f"{self.left} sn içinde yanıt verilmezse otomatik reddedilir.")
        self.left -= 1
        self.timer = self.dlg.after(1000, self._tick)

    def decide(self, allow: bool):
        if self.done:
            return
        self.done, self.result = True, allow
        if self.dlg is not None:
            if self.timer is not None:
                self.dlg.after_cancel(self.timer)
            self.dlg.destroy()
        self.ev.set()


class _HostUI(_Bridge):
    def __init__(self, app, panel):
        super().__init__(app, panel)
        self.pending = []  # bekleyen _Consent'ler

    def log(self, text): self._call("on_log", text)
    def ready(self, conn_id): self._call("on_ready", conn_id)
    def error(self, text): self._call("on_error", text)
    def connected(self, name, addr): self._call("on_connected", name, addr)
    def disconnected(self): self._call("on_disconnected")
    def stats(self, total): self._call("on_stats", total)

    def consent(self, name, addr) -> bool:
        """Motor iş parçacığında bloklar (<= ~32 sn); True = izin ver VE hatırla."""
        if self.dead or self.app.closing:
            return False
        c = _Consent()
        self.pending.append(c)
        self.app.post(lambda: c.show(self.app, name, addr))
        if not c.ev.wait(CONSENT_SECONDS + 2):
            self.app.post(lambda: c.decide(False))  # Tk'ye iş parçacığından dokunma: kapanışı kuyruğa bırak
        self.pending.remove(c)
        return c.result

    def cancel_consent(self):
        """Tk iş parçacığı: Host durdu ya da pencere kapandı, bekleyen istek reddedilir."""
        for c in list(self.pending):
            c.decide(False)


class _ClientUI(_Bridge):
    viewer = None

    def status(self, text): self._call("on_status", text)
    def closed(self, code, text, fatal): self._call("on_closed", code, text, fatal)

    def frame(self, img):
        """Motor iş parçacığında: yeniden ölçekleme burada yapılır (Tk iş parçacığı yalnızca paste eder)."""
        if not self.dead and self.viewer is not None:
            try:
                self.viewer.put(img)
            except Exception:
                _log(traceback.format_exc())


# --- görüntüleyici ---

class Viewer:
    """Karşı ekranı gösteren pencere; fare ve klavyeyi Host'a yollar. Tk tarafı yalnızca paste eder."""

    def __init__(self, app, title, send, on_close, on_shown):
        self.send, self.on_close, self.on_shown = send, on_close, on_shown
        self.keys = KeyMapper(send)
        self.pressed = set()  # Host'a iletilen basılı fare düğmeleri
        self.closed = self.shown = False
        self.shown_at = None  # time.monotonic() ile ilk kare anı
        self.top = top = tk.Toplevel(app.root)
        top.withdraw()  # ilk kare gelene kadar görünmez
        top.title(title)
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        w, h = min(1280, int(sw * 0.8)), min(780, int(sh * 0.8))
        top.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        top.minsize(app.px(480), app.px(320))
        top.protocol("WM_DELETE_WINDOW", self.close)
        bar = ttk.Frame(top, padding=(app.px(8), app.px(4)))
        bar.pack(side="bottom", fill="x")
        self.status_lbl = ttk.Label(bar, text="Bağlanıyor…")  # StringVar yok: motor iş parçacığı bu nesneyi bırakırsa __del__ Tk'ye dokunurdu
        self.status_lbl.pack(side="left")
        self.timer_lbl = ttk.Label(bar, text="")  # geçen bağlantı süresi, ilk kare gelince başlar
        self.timer_lbl.pack(side="left", padx=(app.px(16), 0))
        ttk.Button(bar, text="Bağlantıyı Kes", command=self.close, takefocus=False).pack(side="right")
        self.canvas = c = tk.Canvas(top, bg="black", highlightthickness=0, takefocus=True)
        c.pack(fill="both", expand=True)
        self._lock = threading.Lock()  # _csize, _last, _pending, _refit
        self._csize = (w, h - app.px(40))
        self._last = self._pending = None  # son ham kare; Tk'nin çizeceği (ölçeklenmiş kare, dikdörtgen)
        self._refit = False
        self._photo = self._item = None
        self._rect = None  # çizilen karenin tuvaldeki (sol, üst, genişlik, yükseklik)'i; fare eşlemesi buna göre
        c.bind("<Configure>", self._on_configure)
        c.bind("<Motion>", self._on_motion)
        for n, b in ((1, "l"), (2, "m"), (3, "r")):
            c.bind(f"<ButtonPress-{n}>", lambda e, b=b: self._on_button(e, b, 1))
            c.bind(f"<ButtonRelease-{n}>", lambda e, b=b: self._on_button(e, b, 0))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<KeyPress>", self._on_key_press)
        c.bind("<KeyRelease>", self._on_key_release)
        c.bind("<FocusOut>", self._on_focus_out)
        self._after = top.after(VIEW_MS, self._tick)
        self._timer_after = top.after(ELAPSED_MS, self._tick_timer)

    # motor iş parçacığı
    def put(self, img):
        with self._lock:
            self._last, size = img, self._csize  # <Configure> yeniden sığdırabilsin: değişmeyen ekran kare yollamaz
        scaled = self._fit(img, *size)
        with self._lock:
            self._pending = scaled

    @staticmethod
    def _fit(img, cw, ch):
        dw, dh, x, y = fit_rect(img.width, img.height, cw, ch)
        if (dw, dh) != img.size:
            img = img.resize((dw, dh), Image.Resampling.BILINEAR)
        return img, (x, y, dw, dh)

    # Tk iş parçacığı
    def set_status(self, text):
        if not self.closed:
            self.status_lbl.configure(text=text)

    def _tick(self):
        try:
            with self._lock:
                pend, self._pending = self._pending, None
                refit, self._refit = self._refit, False
                last, size = self._last, self._csize
            # shortcut: pencere boyutu değişince son kare Tk iş parçacığında yeniden ölçeklenir (nadir), yükseltme: iş parçacığına taşı
            if refit and last is not None:
                pend = self._fit(last, *size)
            if pend is not None:
                self._draw(*pend)
        except Exception:
            _log(traceback.format_exc())
        finally:
            if not self.closed:
                self._after = self.top.after(VIEW_MS, self._tick)

    def _tick_timer(self):
        try:
            if self.shown_at is not None:
                self.timer_lbl.configure(text=f"Süre: {fmt_duration(time.monotonic() - self.shown_at)}")
        except Exception:
            _log(traceback.format_exc())
        finally:
            if not self.closed:
                self._timer_after = self.top.after(ELAPSED_MS, self._tick_timer)

    def _draw(self, img, rect):
        x, y, dw, dh = rect
        if self._photo is None or (self._photo.width(), self._photo.height()) != (dw, dh):
            self._photo = ImageTk.PhotoImage(img, master=self.top)  # yalnızca ilk karede ya da boyut değişince
            if self._item is None:
                self._item = self.canvas.create_image(x, y, anchor="nw", image=self._photo)
            else:
                self.canvas.itemconfigure(self._item, image=self._photo)
        else:
            self._photo.paste(img)
        self.canvas.coords(self._item, x, y)
        self._rect = rect
        if not self.shown:
            self.shown = True
            self.shown_at = time.monotonic()
            self.timer_lbl.configure(text=f"Süre: {fmt_duration(0)}")
            self.top.deiconify()
            self.top.lift()
            self.canvas.focus_force()
            self.on_shown()

    def _on_configure(self, e):
        if e.width < 16 or e.height < 16:
            return
        with self._lock:
            if (e.width, e.height) != self._csize:
                self._csize, self._refit = (e.width, e.height), True

    def _norm(self, x, y):
        """Tuval koordinatı -> çizilen karenin içinde [0,1] (dışarısı kenara sıkıştırılır); kare yoksa None."""
        if self._rect is None:
            return None
        ox, oy, dw, dh = self._rect
        return (round(min(max((x - ox + 0.5) / dw, 0.0), 1.0), 4), round(min(max((y - oy + 0.5) / dh, 0.0), 1.0), 4))

    def _inside(self, x, y):
        return self._rect is not None and self._rect[0] <= x < self._rect[0] + self._rect[2] \
            and self._rect[1] <= y < self._rect[1] + self._rect[3]

    def _on_motion(self, e):
        p = self._norm(e.x, e.y)
        if p:
            self.send({"t": "m", "x": p[0], "y": p[1]})

    def _on_button(self, e, b, down):
        self.canvas.focus_set()  # klavye olayları tuvale gitsin
        if down:
            if not self._inside(e.x, e.y):
                return  # siyah şeride tıklama Host'ta kenara tıklamaya dönüşmesin
            self.pressed.add(b)
        elif b in self.pressed:
            self.pressed.discard(b)
        else:
            return  # basışı iletilmemiş düğmenin bırakışı da iletilmez
        self.keys.before_mouse()
        x, y = self._norm(e.x, e.y)
        self.send({"t": "b", "b": b, "d": down, "x": x, "y": y})

    def _on_wheel(self, e):
        if e.delta:
            self.keys.before_mouse()
            self.send({"t": "w", "d": max(-1200, min(1200, int(e.delta)))})  # Host da sıkıştırır; küçük değerler olduğu gibi

    def _on_key_press(self, e):
        self.keys.key_press(e.keysym, e.keycode, e.char)
        return "break"  # Tk'nin Tab gezintisi gibi varsayılanları çalışmasın

    def _on_key_release(self, e):
        self.keys.key_release(e.keysym, e.keycode)
        return "break"

    def _on_focus_out(self, e):
        self.keys.focus_out()

    def close(self, notify=True):
        if self.closed:
            return
        self.closed = True
        self.keys.focus_out()  # Host'ta basılı tuş kalmasın
        self.top.after_cancel(self._after)
        self.top.after_cancel(self._timer_after)
        self.top.destroy()
        self._photo = None  # PhotoImage Tk iş parçacığında bırakılsın (motor iş parçacığı Viewer'a son referansı tutabilir)
        if notify:
            self.on_close()


# --- ekranlar ---

class Launcher(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.root, padding=app.px(28))
        P = app.px
        ttk.Label(self, text=TITLE, style="Head.TLabel").pack(anchor="w")
        ttk.Label(self, style="Hint.TLabel", wraplength=P(460), text=(
            "Basit uzak masaüstü: karşı ekranı izle, fare ve klavyeyle yönet.")
        ).pack(anchor="w", pady=(P(4), P(20)))
        self.host_btn = ttk.Button(self, text="Bu Bilgisayarın İzlenmesine İzin Ver (Sunucu/Host)", style="Big.TButton",
                                   command=lambda: app.show(app.host_panel))
        self.host_btn.pack(fill="x", pady=(0, P(10)))
        self.client_btn = ttk.Button(self, text="Uzaktaki Bilgisayarı Yönet (İstemci/Client)", style="Big.TButton",
                                     command=lambda: app.show(app.client_panel))
        self.client_btn.pack(fill="x")


class HostPanel(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.root, padding=app.px(16))
        P = self.P = app.px
        self.app, self.cfg = app, app.cfg
        self.host = self.ui = None  # çalışan motor ve onun ui köprüsü
        self.ready, self.sent, self.gen = False, None, ""  # gen: son üretilen şifre
        self.who, self.since, self._elapsed_after = "", None, None  # bağlı kişi, bağlanma anı (monotonic), sayaç after kimliği
        self.mode = tk.StringVar(value="relay" if self.cfg["mode"] == "relay" else "ngrok")
        self.token = tk.StringVar(value=self.cfg["ngrok_token"])
        self.relay = tk.StringVar(value=self.cfg["relay"])
        self.relay_token = tk.StringVar(value=self.cfg["relay_token"])
        self.conn, self.pw = tk.StringVar(), tk.StringVar()
        self.state, self.info, self.log = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.columnconfigure(1, weight=1)
        wrap = P(540)

        self.back = ttk.Button(self, text="← Geri", command=lambda: app.show(app.launcher))
        self.back.grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Sunucu (Host): bu bilgisayarı paylaş", style="Head.TLabel").grid(
            row=0, column=1, columnspan=2, sticky="w", padx=P(12))
        self.banner = tk.Label(self, bg="#b3261e", fg="white", font=("Segoe UI", 12, "bold"), pady=P(8))
        self.banner.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(P(12), 0))
        self.banner.grid_remove()

        modes = ttk.Frame(self)
        modes.grid(row=2, column=0, columnspan=3, sticky="w", pady=(P(12), P(4)))
        ttk.Label(modes, text="Bağlantı yöntemi:").pack(side="left")
        self.rb_ngrok = ttk.Radiobutton(modes, text="ngrok (varsayılan)", value="ngrok", variable=self.mode)
        self.rb_relay = ttk.Radiobutton(modes, text="Kendi relay sunucum", value="relay", variable=self.mode)
        self.rb_ngrok.pack(side="left", padx=(P(12), P(8)))
        self.rb_relay.pack(side="left")

        self.ngrok_box = box = ttk.LabelFrame(self, text="ngrok", padding=P(10))
        box.grid(row=3, column=0, columnspan=3, sticky="ew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Authtoken:").grid(row=0, column=0, sticky="w")
        self.token_entry = ttk.Entry(box, textvariable=self.token, show="•")
        self.token_entry.grid(row=0, column=1, sticky="ew", padx=(P(8), 0))
        ttk.Label(box, style="Hint.TLabel", wraplength=wrap - P(24), justify="left", text=(
            "Ücretsiz ngrok hesabı yeterli. Ücretsiz TCP için kart doğrulaması istenir; aylık 1 GB veri sınırı "
            "vardır (yaklaşık 20-25 dk kesintisiz hareket). Authtoken'ı şuradan alın:")
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(P(6), P(2)))
        url = ttk.Entry(box)
        url.insert(0, NGROK_URL)
        url.state(["readonly"])  # seçilip kopyalanabilsin
        url.grid(row=2, column=0, columnspan=2, sticky="ew")

        self.relay_box = box = ttk.LabelFrame(self, text="Kendi relay sunucum", padding=P(10))
        box.grid(row=3, column=0, columnspan=3, sticky="ew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Relay adresi (host:port):").grid(row=0, column=0, sticky="w")
        self.relay_entry = ttk.Entry(box, textvariable=self.relay)
        self.relay_entry.grid(row=0, column=1, sticky="ew", padx=(P(8), 0))
        ttk.Label(box, text="Relay anahtarı (token):").grid(row=1, column=0, sticky="w", pady=(P(6), 0))
        self.relay_token_entry = ttk.Entry(box, textvariable=self.relay_token, show="•")
        self.relay_token_entry.grid(row=1, column=1, sticky="ew", padx=(P(8), 0), pady=(P(6), 0))
        ttk.Label(box, style="Hint.TLabel", wraplength=wrap - P(24), justify="left", text=(
            "Kararlı ID ve sınırsız veri. Sunucunuzda relay.py çalışıyor olmalı (README).")
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(P(6), 0))

        ttk.Label(self, text="Bağlantı ID:").grid(row=4, column=0, sticky="w", pady=(P(14), 0))
        ttk.Entry(self, textvariable=self.conn, state="readonly", font=("Consolas", 12)).grid(
            row=4, column=1, sticky="ew", padx=P(8), pady=(P(14), 0))
        self.copy = ttk.Button(self, text="Kopyala", command=self._copy)
        self.copy.grid(row=4, column=2, pady=(P(14), 0))
        ttk.Label(self, text="Şifre:").grid(row=5, column=0, sticky="w", pady=(P(6), 0))
        self.pw_entry = ttk.Entry(self, textvariable=self.pw, font=("Consolas", 12))
        self.pw_entry.grid(row=5, column=1, sticky="ew", padx=P(8), pady=(P(6), 0))
        self.regen = ttk.Button(self, text="Yenile", command=self._new_pw)
        self.regen.grid(row=5, column=2, pady=(P(6), 0))
        self.pw_note = ttk.Label(self, wraplength=wrap - P(120), justify="left")
        self.pw_note.grid(row=6, column=1, columnspan=2, sticky="w", padx=P(8))

        bar = ttk.Frame(self)
        bar.grid(row=7, column=0, columnspan=3, sticky="w", pady=(P(14), 0))
        self.btn = ttk.Button(bar, width=18, command=self._toggle)
        self.btn.pack(side="left")
        self.kick = ttk.Button(bar, text="Bağlantıyı Kes", command=self._kick, state="disabled")
        self.kick.pack(side="left", padx=P(8))
        ttk.Button(bar, text="Güvenilenleri Sıfırla", command=self._reset_trust).pack(side="left")
        self.hint = ttk.Label(self, style="Warn.TLabel", wraplength=wrap, justify="left")
        self.hint.grid(row=8, column=0, columnspan=3, sticky="w", pady=(P(4), 0))
        self.state_lbl = ttk.Label(self, textvariable=self.state, font=BOLD, wraplength=wrap, justify="left")
        self.state_lbl.grid(row=9, column=0, columnspan=3, sticky="w", pady=(P(10), 0))
        self.info_lbl = ttk.Label(self, textvariable=self.info)
        self.info_lbl.grid(row=10, column=0, columnspan=3, sticky="w")
        self.log_lbl = ttk.Label(self, textvariable=self.log, style="Hint.TLabel", wraplength=wrap, justify="left")
        self.log_lbl.grid(row=11, column=0, columnspan=3, sticky="w")
        for lbl, var in ((self.info_lbl, self.info), (self.log_lbl, self.log)):  # boş satır boşluk bırakmasın
            var.trace_add("write", lambda *_, l=lbl, v=var: l.grid() if v.get() else l.grid_remove())
            lbl.grid_remove()
        ttk.Label(self, style="Hint.TLabel", wraplength=wrap, justify="left", text=(
            "İlk eşleşmede bu bilgisayarın başındaki biri \"İzin ver\" düğmesine basmalıdır; "
            "sonraki bağlantılar aynı cihazdan sormadan kabul edilir.")
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(P(10), 0))

        self._inputs = [self.rb_ngrok, self.rb_relay, self.token_entry, self.relay_entry, self.relay_token_entry,
                        self.pw_entry, self.regen]
        if self.cfg["k"]:
            self.pw.set(SAVED_PW)  # şifrenin kendisi diskte yok; yalnızca k
        else:
            self._new_pw()
        self._status("Hazır değil. Başlat'a basın.", "hint")
        self._refresh()
        for var in (self.mode, self.token, self.relay, self.relay_token, self.pw):
            var.trace_add("write", self._refresh)

    # --- şifre politikası ---
    def _new_pw(self):
        self.gen = new_password()
        self.pw.set(self.gen)

    def _pw_kind(self):
        raw = self.pw.get()
        if raw == SAVED_PW and self.cfg["k"]:
            return "saved"
        if self.gen and raw == self.gen:  # gen boşken boş metin "üretilmiş" sayılmasın
            return "gen"
        return "custom" if len(raw.strip()) >= MIN_CUSTOM_PW else "short"

    def _blocker(self):
        """Başlatmayı engelleyen sebep ('' = engel yok); şifre sebebi şifre notunda gösterilir."""
        if self.mode.get() == "ngrok":
            if not self.token.get().strip():
                return "ngrok Authtoken girmeden başlatılamaz."
        elif not (parse_relay(self.relay.get()) and self.relay_token.get().strip()):
            return "Relay adresi (host:port) ve anahtarı gerekli."
        return ""

    def _refresh(self, *_):
        running = self.host is not None
        ngrok = self.mode.get() == "ngrok"
        (self.relay_box if ngrok else self.ngrok_box).grid_remove()
        (self.ngrok_box if ngrok else self.relay_box).grid()
        if not running:
            self.conn.set(ID_PLACEHOLDER if ngrok else fmt_id(self.cfg["relay_id"]))
        kind, block = self._pw_kind(), self._blocker()
        text, tone = {
            "saved": ("Kayıtlı şifre kullanılacak. Yeni şifre için Yenile'ye basın.", "hint"),
            "gen": ("Rastgele üretildi (önerilen).", "hint"),
            "custom": ("Kendi şifrenizi seçtiniz. Rastgele üretilen şifreler daha güvenlidir.", "warn"),
            "short": (f"Kendi şifreniz en az {MIN_CUSTOM_PW} karakter olmalı ({len(self.pw.get().strip())}/{MIN_CUSTOM_PW}). "
                      "Rastgele üretilen şifreler daha güvenlidir.", "bad"),
        }[kind]
        self.pw_note.configure(text=text)
        _tone(self.pw_note, tone)
        self.hint.configure(text="" if running else block)
        self.hint.grid() if self.hint.cget("text") else self.hint.grid_remove()
        self.btn.configure(text="Durdur" if running else "Hazır Ol / Başlat")
        self.btn.state(["!disabled" if running or not (block or kind == "short") else "disabled"])
        for w in self._inputs:
            w.state(["disabled" if running else "!disabled"])
        self.back.state(["disabled" if running else "!disabled"])
        self.copy.state(["!disabled" if self.conn.get() not in (ID_PLACEHOLDER, ID_PREPARING) else "disabled"])

    def _status(self, text, tone=""):
        self.state.set(text)
        _tone(self.state_lbl, tone)

    def _info(self):
        parts = []
        if self.sent is not None:
            parts.append(f"Gönderilen: {fmt_bytes(self.sent)}")
        if self.ready:
            parts.append("Uyku engellendi")
        warn = self.sent is not None and self.sent >= WARN_BYTES and self.mode.get() == "ngrok"
        if warn:
            parts.append("ngrok ücretsiz kotası 1 GB/ay, dolmak üzere")
        self.info.set("   ·   ".join(parts))
        _tone(self.info_lbl, "warn" if warn else "")

    # --- düğmeler ---
    def _copy(self):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(self.conn.get())

    def _toggle(self):
        if self.host is not None:
            self.stop()
            return
        kind = self._pw_kind()
        if self._blocker() or kind == "short":
            return
        cfg, mode = self.cfg, self.mode.get()
        cfg.update(mode=mode, ngrok_token=self.token.get().strip(), relay=self.relay.get().strip(),
                   relay_token=self.relay_token.get().strip())
        if kind != "saved":
            # shortcut: scrypt (~80 ms) Tk iş parçacığında, yükseltme: maliyet artarsa iş parçacığına taşı
            cfg["k"] = proto.derive_key(self.pw.get().strip()).hex()
        try:
            config.save(cfg)
        except OSError:
            _log(traceback.format_exc())  # ayarlar kaydedilemese de oturum çalışır
        self.ui = _HostUI(self.app, self)
        try:
            self.host = self.app.host_class()(cfg, self.ui, mode, save=config.save)
            self.ready, self.sent = False, None
            self.conn.set(ID_PREPARING)
            self._status("Başlatılıyor…", "hint")
            self._info()
            self.host.start()
        except Exception as e:
            _log(traceback.format_exc())
            self.stop(f"Başlatılamadı: {e}", "bad")
            return
        self._refresh()

    def stop(self, msg="Durduruldu.", tone="hint"):
        """Bloklamaz: Host.stop() kapatmayı kendi iş parçacığına devreder."""
        host, ui, self.host, self.ui = self.host, self.ui, None, None
        self.ready, self.sent = False, None
        if ui is not None:
            ui.dead = True
            ui.cancel_consent()
        if host is not None:
            try:
                host.stop()
            except Exception:
                _log(traceback.format_exc())
        self._stop_elapsed()
        self.banner.grid_remove()
        self.kick.state(["disabled"])
        self._status(msg, tone)
        self._info()
        self._refresh()

    def _kick(self):
        if self.host is not None:
            self.host.kick()
            self.kick.state(["disabled"])

    def _reset_trust(self):
        self.cfg["trusted"].clear()  # Host aynı listeyi okuyor: yerinde temizle
        try:
            config.save(self.cfg)
        except OSError:
            _log(traceback.format_exc())
        self.log.set("Güvenilen cihaz listesi sıfırlandı: sıradaki bağlantı yeniden onay ister.")

    # --- motor olayları (Tk iş parçacığı; _HostUI köprüsünden) ---
    def on_log(self, text):
        line = " ".join(str(text).split())
        self.log.set(line if len(line) <= 300 else line[:299] + "…")

    def on_ready(self, conn_id):
        self.ready = True
        self.conn.set(fmt_id(conn_id))
        self._status("Hazır: bağlantı bekleniyor.", "good")
        self._info()
        self._refresh()

    def on_error(self, text):
        # Host.ui.error yalnızca kurulum hatasıdır (ngrok, relay ayarı) ve motor ana iş parçacığı hemen biter:
        # boşta duruma dön, kullanıcı düzeltip yeniden Başlat'a bassın
        self.stop(f"Hata: {text}", "bad")

    def _render_banner(self):
        if self.since is not None:
            self.banner.configure(text=f"●  Bağlı: {self.who}  ·  Süre: {fmt_duration(time.monotonic() - self.since)}")

    def _tick_elapsed(self):
        try:
            self._render_banner()
        except Exception:
            _log(traceback.format_exc())
        finally:
            if self.since is not None and not self.app.closing:
                self._elapsed_after = self.after(ELAPSED_MS, self._tick_elapsed)

    def _stop_elapsed(self):
        self.since = None
        if self._elapsed_after is not None:
            self.after_cancel(self._elapsed_after)
            self._elapsed_after = None

    def on_connected(self, name, addr):
        self._stop_elapsed()  # eski sayaç döngüsü varsa kapat: tek döngü kalsın
        self.who, self.since = f"{name}  ({addr})", time.monotonic()
        self._render_banner()
        self._elapsed_after = self.after(ELAPSED_MS, self._tick_elapsed)
        self.banner.grid()
        self.kick.state(["!disabled"])
        self._status(f"Bağlı: {name}", "good")
        if self.app.root.state() == "iconic":
            self.app.root.deiconify()  # görünmez bağlantı olmasın: pencere simge durumundaysa öne gelsin

    def on_disconnected(self):
        self._stop_elapsed()
        self.banner.grid_remove()
        self.kick.state(["disabled"])
        if self.host is not None:
            self._status("Hazır: bağlantı bekleniyor." if self.ready else "Başlatılıyor…", "good" if self.ready else "hint")

    def on_stats(self, total):
        self.sent = total
        self._info()


class ClientPanel(ttk.Frame):
    def __init__(self, app):
        super().__init__(app.root, padding=app.px(16))
        P = self.P = app.px
        self.app, self.cfg = app, app.cfg
        self.client = self.cui = self.viewer = None
        self.conn, self.pw = tk.StringVar(), tk.StringVar()
        self.relay = tk.StringVar(value=self.cfg["relay"])
        self.relay_token = tk.StringVar(value=self.cfg["relay_token"])
        self.state = tk.StringVar()
        self.columnconfigure(1, weight=1)
        wrap = P(540)

        self.back = ttk.Button(self, text="← Geri", command=lambda: app.show(app.launcher))
        self.back.grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="İstemci (Client): uzaktaki bilgisayarı yönet", style="Head.TLabel").grid(
            row=0, column=1, sticky="w", padx=P(12))
        ttk.Label(self, text="Bağlantı ID:").grid(row=1, column=0, sticky="w", pady=(P(16), 0))
        self.id_entry = ttk.Entry(self, textvariable=self.conn, font=("Consolas", 12))
        self.id_entry.grid(row=1, column=1, sticky="ew", padx=(P(8), 0), pady=(P(16), 0))
        ttk.Label(self, style="Hint.TLabel", wraplength=wrap, justify="left", text=(
            "Sunucu penceresindeki ID: ngrok için adres:port, relay için 9 haneli sayı.")
        ).grid(row=2, column=1, sticky="w", padx=(P(8), 0))
        ttk.Label(self, text="Şifre:").grid(row=3, column=0, sticky="w", pady=(P(10), 0))
        self.pw_entry = ttk.Entry(self, textvariable=self.pw, show="•", font=("Consolas", 12))
        self.pw_entry.grid(row=3, column=1, sticky="ew", padx=(P(8), 0), pady=(P(10), 0))

        box = ttk.LabelFrame(self, text="Gelişmiş", padding=P(10))
        box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(P(16), 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Relay adresi (host:port):").grid(row=0, column=0, sticky="w")
        self.relay_entry = ttk.Entry(box, textvariable=self.relay)
        self.relay_entry.grid(row=0, column=1, sticky="ew", padx=(P(8), 0))
        ttk.Label(box, text="Relay anahtarı (token):").grid(row=1, column=0, sticky="w", pady=(P(6), 0))
        self.relay_token_entry = ttk.Entry(box, textvariable=self.relay_token, show="•")
        self.relay_token_entry.grid(row=1, column=1, sticky="ew", padx=(P(8), 0), pady=(P(6), 0))
        ttk.Label(box, style="Hint.TLabel", wraplength=wrap - P(24), justify="left", text=(
            "Yalnızca 9 haneli relay ID'si için gerekir.")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(P(6), 0))

        self.btn = ttk.Button(self, command=self._toggle, width=14)
        self.btn.grid(row=5, column=0, columnspan=2, sticky="w", pady=(P(16), 0))
        self.state_lbl = ttk.Label(self, textvariable=self.state, font=BOLD, wraplength=wrap, justify="left")
        self.state_lbl.grid(row=6, column=0, columnspan=2, sticky="w", pady=(P(10), 0))
        self._inputs = [self.id_entry, self.pw_entry, self.relay_entry, self.relay_token_entry]
        for w in (self.id_entry, self.pw_entry):
            w.bind("<Return>", lambda e: self._toggle() if self.client is None else None)
        self._status("", "hint")
        self._refresh()

    def _refresh(self):
        running = self.client is not None
        self.btn.configure(text="Vazgeç" if running else "Bağlan")
        for w in self._inputs:
            w.state(["disabled" if running else "!disabled"])
        self.back.state(["disabled" if running else "!disabled"])

    def _status(self, text, tone=""):
        self.state.set(text)
        _tone(self.state_lbl, tone)

    def _toggle(self):
        if self.client is not None:
            self.stop("Vazgeçildi.")
            return
        cid, pw, rtxt, rtok = self.conn.get().strip(), self.pw.get().strip(), self.relay.get().strip(), self.relay_token.get().strip()
        relay = parse_relay(rtxt) if rtxt else None
        if not cid or not pw:
            return self._status("Bağlantı ID'si ve şifre gerekli.", "bad")
        if rtxt and relay is None:
            return self._status("Relay adresi host:port biçiminde olmalı.", "bad")
        self.cfg.update(relay=rtxt, relay_token=rtok)
        try:
            config.save(self.cfg)
        except OSError:
            _log(traceback.format_exc())
        self.cui = _ClientUI(self.app, self)
        try:
            self.client = self.app.client_class()(cid, pw, self.cui, self.cfg["uid"],
                                                  relay=(relay[0], relay[1], rtok) if relay else None)
            self.viewer = self.cui.viewer = Viewer(self.app, f"{TITLE} - {cid}", self.client.send_input,
                                                   lambda: self.stop("Bağlantı kapatıldı."), self._on_shown)
            self._status("Bağlanıyor…", "hint")
            self._refresh()
            self.client.start()
        except Exception as e:
            _log(traceback.format_exc())
            self.stop(f"Bağlanılamadı: {e}", "bad")

    def stop(self, msg="", tone="hint"):
        """Bloklamaz: Client.stop() soketi kapatıp döner."""
        client, cui, viewer = self.client, self.cui, self.viewer
        self.client = self.cui = self.viewer = None
        if cui is not None:
            cui.dead, cui.viewer = True, None  # Viewer'a son referans motor iş parçacığında kalmasın
        if viewer is not None:
            viewer.close(notify=False)
        if client is not None:
            try:
                client.stop()
            except Exception:
                _log(traceback.format_exc())
        self._status(msg, tone)
        self._refresh()

    def _on_shown(self):
        self._status("Bağlandı.", "good")

    # --- motor olayları (Tk iş parçacığı; _ClientUI köprüsünden) ---
    def on_status(self, text):
        self._status(text, "hint")
        if self.viewer is not None:
            self.viewer.set_status(text)

    def on_closed(self, code, text, fatal):
        if code == "stopped":
            return
        if fatal:
            self.stop(text, "bad")  # motor yeniden denemeyecek: görüntüleyici kapanır, hata panelde kalır
        else:
            self.on_status(text)  # motor yeniden deniyor


class App:
    def __init__(self, root=None, host_cls=None, client_cls=None):
        self.root = root or tk.Tk()
        self.host_cls, self.client_cls = host_cls, client_cls
        self.cfg = config.load()
        self.q = queue.Queue()
        self.closing = False
        self._baseline = set(threading.enumerate())  # kapanışta yalnızca sonradan doğanlar (motorlar) beklenir
        r = self.root
        r.title(TITLE)
        scale = max(1.0, r.winfo_fpixels("1i") / 96)  # piksel cinsinden boşluklar DPI ile büyür; yazı tipleri zaten pt
        self.px = lambda n: round(n * scale)
        _setup_style(r, self.px)
        self.launcher, self.host_panel, self.client_panel = Launcher(self), HostPanel(self), ClientPanel(self)
        r.protocol("WM_DELETE_WINDOW", self.close)
        self.show(self.launcher)
        self._pump_id = r.after(POLL_MS, self._pump)

    def host_class(self):
        if self.host_cls is None:
            from .host import Host  # tembel: arayüz testleri sahte motorla çalışır
            self.host_cls = Host
        return self.host_cls

    def client_class(self):
        if self.client_cls is None:
            from .client import Client
            self.client_cls = Client
        return self.client_cls

    def post(self, fn):
        """Her iş parçacığından çağrılabilir: fn Tk iş parçacığında çalışır."""
        self.q.put(fn)

    def _pump(self):
        try:
            while True:
                try:
                    fn = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception:
                    _log(traceback.format_exc())
        finally:
            if not self.closing:
                self._pump_id = self.root.after(POLL_MS, self._pump)  # hata olsa da yeniden kurulur

    def show(self, frame):
        for f in (self.launcher, self.host_panel, self.client_panel):
            f.pack_forget()
        frame.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    def run(self):
        self.root.mainloop()

    def close(self):
        """Motorları durdurur (stop() bloklamaz), kapatma iş parçacıklarını en çok CLOSE_WAIT bekler, sonra pencereyi yıkar."""
        if self.closing:
            return
        self.closing = True
        self.root.withdraw()  # beklerken pencere hemen kaybolsun
        self.root.after_cancel(self._pump_id)
        self.host_panel.stop("")
        self.client_panel.stop("")
        deadline = time.monotonic() + CLOSE_WAIT
        for t in set(threading.enumerate()) - self._baseline - {threading.current_thread()}:
            t.join(max(0.0, deadline - time.monotonic()))  # basılı tuşları bırakan oturum finally'si de bunların içinde
        self.root.destroy()


def main():
    _fix_streams()
    winio.set_dpi_aware()  # ilk Tk penceresinden ve mss örneğinden önce
    app = App()
    _install_hooks(app.root)
    app.run()
