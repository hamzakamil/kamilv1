"""Kabul H, Bölüm 1: Host tarafı GERÇEK girdi kanıtı (opt-in).

Kendi tam ekran, kenarlıksız, her zaman üstte penceresine gerçek winio.Injector ile fare ve klavye gönderir
ve etkisini pencerenin kendi bileşenlerinden okur. Doğrudan çalıştırılmaz: tests/test_input_real.py bunu alt süreç
olarak başlatır. UZAK_REAL_INPUT=1 yoksa hiçbir şey göndermeden çıkar.
Çalışırken fareye ve klavyeye dokunmayın (Esc = iptal, en fazla 20 sn).
Çıkış kodları: 0 geçti, 1 başarısız, 2 izin yok, 4 başlamadan iptal, 5 girdi başladıktan sonra iptal, 6 zaman aşımı.
"""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes as wt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # python tests/... ile çalışınca paket bulunsun
from uzakmasa import winio  # noqa: E402

WATCHDOG_SECONDS = 20
TYPED = "Merhaba şğüöçıİ ÇĞÜÖŞ @€ 123 !'^+%&/()=?_-"
LINE1 = "Kamil V1 surukle-sec deneme satiri"

VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL = 0x08, 0x09, 0x0D, 0x10, 0x11
VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_DELETE = 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E
VK_A, VK_C, VK_V = 0x41, 0x43, 0x56
# basılı kalırsa zarar verenler: genel, sol ve sağ Shift/Ctrl/Alt (+ fare düğmeleri)
STUCK_VKS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x01, 0x02, 0x04)
GA_ROOT, GWL_EXSTYLE, WS_EX_TOPMOST = 2, -20, 0x8

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.GetForegroundWindow.restype = wt.HWND
u32.WindowFromPoint.argtypes = [wt.POINT]
u32.WindowFromPoint.restype = wt.HWND
u32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
u32.GetAncestor.restype = wt.HWND
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsWindowVisible.restype = wt.BOOL
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.GetWindowRect.restype = wt.BOOL
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = wt.LONG
u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u32.GetCursorPos.restype = wt.BOOL
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetSystemMetrics.restype = ctypes.c_int


class Abort(Exception):
    """Güvenlik korumasından biri tuttu: bundan sonra hiçbir girdi gönderilmez."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


class Rig:
    def __init__(self, inj, fullscreen=True):
        self.inj, self.fullscreen = inj, fullscreen
        self.w, self.h = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)  # mutlak hareket birincil monitöre eşlenir
        self.clicks, self.seen, self.cancel, self.started = 0, set(), False, False
        self.root = root = tk.Tk()
        root.title("Kamil V1 gerçek girdi testi")
        root.configure(bg="#1b2530")
        if fullscreen:
            root.geometry("+0+0")  # birincil monitörde aç: tam ekran pencerenin bulunduğu monitörü kaplar
            root.attributes("-fullscreen", True)
        else:
            root.geometry("1000x700+40+40")  # yalnız Rig'i girdi göndermeden denemek için küçük pencere
        root.attributes("-topmost", True)  # -fullscreen'den sonra: pencere yeniden kurulursa bayrak kaybolmasın
        tk.Label(root, bg="#1b2530", fg="white", font=("Segoe UI", 15),
                 text="Gerçek girdi testi: fareye ve klavyeye dokunmayın (Esc = iptal, en fazla 20 sn)").place(relx=0.5, rely=0.02, anchor="n")
        self.entry = tk.Entry(root, font=("Consolas", 20))
        self.entry.place(relx=0.15, rely=0.08, relwidth=0.7, height=50)
        self.text = tk.Text(root, font=("Consolas", 16), wrap="none")
        self.text.place(relx=0.15, rely=0.18, relwidth=0.7, relheight=0.42)
        self.text.insert("1.0", LINE1 + "\n" + "\n".join(f"satir {i}" for i in range(2, 301)))
        self.btn = tk.Button(root, text="Tıkla", font=("Segoe UI", 18), command=self._on_click)
        self.btn.place(relx=0.5, rely=0.66, anchor="n", width=260, height=80)
        self.status = tk.Label(root, bg="#1b2530", fg="white", font=("Segoe UI", 14), text="Tıklama: 0")
        self.status.place(relx=0.5, rely=0.80, anchor="n")
        self.btn.bind("<Double-Button-1>", lambda e: self.seen.add("double"), add="+")
        self.btn.bind("<Button-3>", lambda e: self.seen.add("right"))
        self.entry.bind("<Return>", lambda e: self.seen.add("enter"))
        # Ctrl+A/C/V davranışını burada biz tanımlarız: Tk Windows'ta Ctrl+A'yı "tümünü seç" yapmaz, Tk panosu da
        # süreç bitince boşalır (kullanıcının gerçek panosu silinirdi); kanıt konusu tuş birleşiminin uygulamaya ulaşması
        self.buf = ""
        for key, fn in (("a", self._select_all), ("c", self._copy), ("v", self._paste)):
            for k in (key, key.upper()):  # Caps Lock açıksa Tk büyük harf keysym'i verir
                self.entry.bind(f"<Control-{k}>", fn)
        root.bind("<Escape>", lambda e: setattr(self, "cancel", True))
        root.update()
        root.lift()
        root.focus_force()
        root.update()

    # --- Windows tarafı (deneme çalıştırması bunları değiştirebilir) ---

    def cursor(self):
        p = wt.POINT()
        u32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def is_foreground(self):
        return self.top(u32.GetForegroundWindow()) == self.me()

    def async_down(self, vk):
        return bool(u32.GetAsyncKeyState(vk) & 0x8000)

    def focused(self):
        return self.root.focus_get()

    @staticmethod
    def top(hwnd):
        return u32.GetAncestor(hwnd, GA_ROOT) if hwnd else None

    def me(self):
        return self.top(self.root.winfo_id())  # Tk sarmalayıcıyı yeniden kurabilir: her seferinde yeniden hesapla

    # --- Tk yardımcıları ---

    def _on_click(self):
        self.clicks += 1
        self.status.config(text=f"Tıklama: {self.clicks}")

    def _select_all(self, event):
        self.entry.selection_range(0, "end")
        self.entry.icursor("end")
        return "break"

    def _copy(self, event):
        self.buf = self.sel()
        return "break"

    def _paste(self, event):
        self.entry.insert("insert", self.buf)
        return "break"

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def wait(self, cond, what, timeout=2.0):
        end = time.monotonic() + timeout
        while True:
            self.root.update()
            if cond():
                return
            if time.monotonic() > end:
                raise AssertionError(f"{what} (zaman aşımı)")
            time.sleep(0.01)

    def center(self, widget):
        return widget.winfo_rootx() + widget.winfo_width() // 2, widget.winfo_rooty() + widget.winfo_height() // 2

    def char_xy(self, index):
        """Metin kutusunda bir karakterin sol kenarının hemen sağı (en yakın aralık o karakterin başı olur)."""
        x, y, _, h = self.text.bbox(index)
        return self.text.winfo_rootx() + x + 2, self.text.winfo_rooty() + y + h // 2

    def norm(self, x, y):
        return (x + 0.5) / self.w, (y + 0.5) / self.h  # piksel merkezi: mutlak eşleme aynı piksele düşer

    # --- koruma: her girdiden önce ---

    def check(self, point=None, foreground=True):
        code = 5 if self.started else 4
        if self.cancel:
            raise Abort("Esc ile iptal edildi", code)
        me = self.me()
        if not me or not u32.IsWindowVisible(me):
            raise Abort("test penceresi görünür değil", code)
        if foreground and not self.is_foreground():
            raise Abort("ön plandaki pencere test penceresi değil", code)
        if point and self.top(u32.WindowFromPoint(wt.POINT(*point))) != me:
            raise Abort(f"{point} noktasındaki pencere test penceresi değil", code)

    def preflight(self):
        """Hiçbir şey göndermeden önce: pencere görünür, üstte, ekranı kaplıyor, düğmenin noktası ona ait."""
        me = self.me()
        r = wt.RECT()
        u32.GetWindowRect(me, ctypes.byref(r))
        if self.fullscreen and not (r.left <= 0 and r.top <= 0 and r.right >= self.w and r.bottom >= self.h):
            raise Abort(f"pencere ekranı kaplamıyor: ({r.left},{r.top},{r.right},{r.bottom})", 4)
        if not u32.GetWindowLongW(me, GWL_EXSTYLE) & WS_EX_TOPMOST:
            raise Abort("pencere her zaman üstte değil", 4)
        self.check(self.center(self.btn), foreground=False)  # ön plan şartı ilk tıklamadan sonra

    def wait_foreground(self):
        end = time.monotonic() + 1.5
        while not self.is_foreground():
            if time.monotonic() > end:
                raise Abort("ilk tıklamadan sonra test penceresi ön plana geçmedi", 5)
            self.root.update()
            time.sleep(0.01)

    # --- girdi ---

    def goto(self, x, y, foreground=True):
        self.check((x, y), foreground)
        self.inj.move(*self.norm(x, y))
        self.pump(0.05)
        cx, cy = self.cursor()
        expect(abs(cx - x) <= 1 and abs(cy - y) <= 1, f"imleç ({cx},{cy}), hedef ({x},{y})")

    def click(self, x, y):
        self.goto(x, y)
        self.inj.button("l", True)
        self.inj.button("l", False)

    def tap(self, *vks):
        for vk in vks:
            self.check()
            self.inj.key(vk, True)
            self.inj.key(vk, False)
            self.pump(0.03)

    def chord(self, mod, vk):
        self.check()
        self.inj.key(mod, True)
        self.tap(vk)
        self.inj.key(mod, False)
        self.pump(0.03)

    def sel(self):
        """Entry'deki seçili metin ('' = seçim yok)."""
        if not self.entry.selection_present():
            return ""
        return self.entry.get()[self.entry.index("sel.first"):self.entry.index("sel.last")]

    # --- adımlar ---

    def run(self):
        inj, entry, text = self.inj, self.entry, self.text
        self.preflight()
        bx, by = self.center(self.btn)

        # 1. fare düğmenin üstüne gider, sol tık; ilk tıklamadan sonra pencere ön planda olmalı
        self.goto(bx, by, foreground=False)
        self.started = True
        inj.button("l", True)
        inj.button("l", False)
        self.wait_foreground()
        self.wait(lambda: self.clicks == 1, "ilk tıklama sayacı 1 yapmalı")
        step("fare hareketi + sol tık")

        # 2. çift tık (öncekiyle çift tık sayılmasın diye çift tık süresinden uzun bekle)
        self.pump(0.7)
        self.check((bx, by))
        nx, ny = self.norm(bx, by)
        for _ in range(2):
            inj.button("l", True, nx, ny)
            inj.button("l", False, nx, ny)
        self.wait(lambda: self.clicks == 3, "çift tık sayacı 3 yapmalı")
        expect("double" in self.seen, "Tk <Double-Button-1> görmedi")
        step("çift tık")

        # 3. sağ tık görülür, sayaç artmaz
        self.check((bx, by))
        inj.button("r", True, nx, ny)
        inj.button("r", False, nx, ny)
        self.wait(lambda: "right" in self.seen, "sağ tık görülmedi")
        self.pump(0.1)
        expect(self.clicks == 3, f"sağ tık sayacı değiştirdi: {self.clicks}")
        step("sağ tık")

        # 4. metin kutusunda sürükleyerek seç: bas, hareket et, bırak
        i, j = 5, 9
        (x0, y0), (x1, y1) = self.char_xy(f"1.{i}"), self.char_xy(f"1.{j}")
        self.check((x0, y0))
        inj.button("l", True, *self.norm(x0, y0))
        for k in range(1, 7):
            px, py = x0 + (x1 - x0) * k // 6, y0 + (y1 - y0) * k // 6
            self.check((px, py))
            inj.move(*self.norm(px, py))
            self.pump(0.02)
        inj.button("l", False, *self.norm(x1, y1))
        self.wait(lambda: bool(text.tag_ranges("sel")), "sürükleyerek seçim oluşmadı")
        got = text.get("sel.first", "sel.last")
        expect(got == LINE1[i:j], f"seçilen {got!r}, beklenen {LINE1[i:j]!r}")
        step("sürükleyerek seçme")

        # 5. tekerlek: aşağı kaydırır, sonra yukarı
        cx, cy = self.center(text)
        self.goto(cx, cy)
        before = text.yview()[0]
        for _ in range(3):
            self.check()
            inj.wheel(-120)
            self.pump(0.05)
        self.wait(lambda: text.yview()[0] > before, "tekerlek aşağı kaydırmadı")
        mid = text.yview()[0]
        for _ in range(3):
            self.check()
            inj.wheel(120)
            self.pump(0.05)
        self.wait(lambda: text.yview()[0] < mid, "tekerlek yukarı kaydırmadı")
        step("tekerlek")

        # 6. metin yazma: birebir eşitlik
        ex, ey = self.center(entry)
        self.click(ex, ey)
        self.wait(lambda: self.focused() is entry, "tıklama Entry'ye odak vermedi")
        for ch in TYPED:
            self.check()
            inj.char(ch)
        self.pump(0.3)
        expect(entry.get() == TYPED, f"yazılan {entry.get()!r}, beklenen {TYPED!r}")
        step("Türkçe metin yazma")

        # 7. VK tuşları, görünür etkileriyle (imleç sonda)
        t = TYPED
        self.tap(VK_BACK)
        t = t[:-1]
        self.wait(lambda: entry.get() == t, "BackSpace son karakteri silmedi")
        self.tap(VK_HOME)
        self.check()
        inj.char("X")
        t = "X" + t
        self.wait(lambda: entry.get() == t, "Home imleci başa götürmedi")
        self.tap(VK_DELETE)
        t = "X" + t[2:]
        self.wait(lambda: entry.get() == t, "Delete imlecin sağındaki karakteri silmedi")
        self.tap(VK_END)
        self.wait(lambda: entry.index("insert") == len(t), "End imleci sona götürmedi")
        self.tap(VK_LEFT, VK_LEFT)
        self.wait(lambda: entry.index("insert") == len(t) - 2, "Sol ok imleci kaydırmadı")
        self.tap(VK_RIGHT)
        self.wait(lambda: entry.index("insert") == len(t) - 1, "Sağ ok imleci kaydırmadı")
        step("BackSpace / Home / Delete / End / Sol-Sağ ok")

        self.tap(VK_END)
        self.chord(VK_SHIFT, VK_LEFT)
        self.chord(VK_SHIFT, VK_LEFT)
        self.chord(VK_SHIFT, VK_LEFT)
        self.wait(lambda: self.sel() == t[-3:], "Shift+Sol üç karakteri seçmedi")
        step("Shift+Sol seçimi")

        self.chord(VK_CONTROL, VK_A)
        self.wait(lambda: self.sel() == t, "Ctrl+A tümünü seçmedi")
        self.chord(VK_CONTROL, VK_C)
        self.wait(lambda: self.buf == t, "Ctrl+C seçili metni kopyalamadı")
        self.tap(VK_END)  # seçimi kaldırır, imleç sona
        self.chord(VK_CONTROL, VK_V)
        self.wait(lambda: entry.get() == t + t, "Ctrl+V kopyalanan metni yapıştırmadı")
        step("Ctrl+A / Ctrl+C / Ctrl+V")

        self.tap(VK_RETURN)
        self.wait(lambda: "enter" in self.seen, "Enter Entry'ye ulaşmadı")
        step("Enter")

        # 8. Yukarı/Aşağı ok: metin kutusunda satır değiştirir
        text.see("1.0")
        self.pump(0.1)
        lx, ly = self.char_xy("3.0")
        self.click(lx, ly)
        self.wait(lambda: text.index("insert").startswith("3."), "tıklama imleci 3. satıra koymadı")
        self.tap(VK_UP)
        self.wait(lambda: text.index("insert").startswith("2."), "Yukarı ok satır değiştirmedi")
        self.tap(VK_DOWN)
        self.wait(lambda: text.index("insert").startswith("3."), "Aşağı ok satır değiştirmedi")
        step("Yukarı/Aşağı ok")

        # 9. Tab: odağı Entry'den sonraki bileşene (metin kutusu) taşır
        self.click(ex, ey)
        self.wait(lambda: self.focused() is entry, "tıklama Entry'ye odak vermedi")
        self.tap(VK_TAB)
        self.wait(lambda: self.focused() is text, "Tab odağı metin kutusuna taşımadı")
        step("Tab")

        # 10. release_all: basılı kalan hiçbir şey bırakmaz
        self.check()
        inj.key(VK_SHIFT, True)
        inj.key(VK_CONTROL, True)
        inj.button("l", True)
        self.wait(lambda: all(self.async_down(v) for v in (VK_SHIFT, VK_CONTROL, 0x01)), "sınama öncesi tuşlar basılı görünmüyor")
        inj.release_all()
        self.wait(lambda: not any(self.async_down(v) for v in STUCK_VKS), "release_all sonrası bir tuş/düğme hâlâ basılı")
        step("release_all")


def expect(cond, what):
    if not cond:
        raise AssertionError(what)


def step(name):
    print(f"[tamam] {name}", flush=True)


def watchdog(inj):
    time.sleep(WATCHDOG_SECONDS)
    print(f"ZAMAN ASIMI: {WATCHDOG_SECONDS} sn doldu, süreç sonlandırılıyor", flush=True)
    try:
        inj.release_all()  # sert çıkıştan önce basılı kalan tuş/düğme bırakılsın
    except Exception:
        pass
    os._exit(6)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.environ.get("UZAK_REAL_INPUT") != "1":
        print("UZAK_REAL_INPUT=1 yok: gerçek girdi testi çalıştırılmadı, hiçbir girdi gönderilmedi", flush=True)
        return 2
    winio.set_dpi_aware()  # ilk Tk penceresinden önce
    inj = winio.Injector()
    threading.Thread(target=watchdog, args=(inj,), daemon=True).start()
    rig = None
    try:
        rig = Rig(inj)
        rig.run()
        print("SONUC: GECTI", flush=True)
        return 0
    except Abort as e:
        print(f"IPTAL ({e.code}): {e}. Bundan sonra girdi gönderilmedi", flush=True)
        return e.code
    except Exception:
        print("SONUC: KALDI\n" + traceback.format_exc(), flush=True)
        return 1
    finally:
        inj.release_all()
        if rig:
            try:
                rig.root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
