"""Windows katmanı: DPI, ekran yakalama, JPEG, kilit algısı, uyku engeli, girdi enjeksiyonu."""
import ctypes
import io
from ctypes import wintypes as wt

import mss
from PIL import Image

# Paylaşılan windll önbelleği yerine özel kopyalar (argtypes başkasını bozmasın)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32")

GENERIC_READ = 0x80000000
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x0800, 0x8000
_BUTTONS = {"l": (0x0002, 0x0004), "r": (0x0008, 0x0010), "m": (0x0020, 0x0040)}  # (aşağı, yukarı)
# MapVirtualKeyW gezinti kümesini sayısal tuş takımından ayıramaz: genişletilmiş bayrağı listeden gelir
_EXTENDED = frozenset({0x03, *range(0x21, 0x29), 0x2D, 0x2E, *range(0x5B, 0x5E), 0x5F, 0x6F, 0x90, 0xA3, 0xA5})

ULONG_PTR = ctypes.c_size_t  # wintypes'ta yok; 64 bitte c_ulong 4 bayttır, yapı boyutunu bozar


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]  # HARDWAREINPUT yok: en büyük üye MOUSEINPUT


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _InputUnion)]  # 64 bitte sizeof == 40


_user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wt.UINT
_user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_user32.MapVirtualKeyW.restype = wt.UINT
_user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_user32.OpenInputDesktop.restype = wt.HANDLE
_user32.CloseDesktop.argtypes = [wt.HANDLE]
_user32.CloseDesktop.restype = wt.BOOL
_kernel32.SetThreadExecutionState.argtypes = [wt.DWORD]
_kernel32.SetThreadExecutionState.restype = wt.DWORD


def set_dpi_aware() -> None:
    """İlk Tk penceresinden ve mss örneğinden önce çağrılır: ilk çağrı kazanır, mss kurucusunun kendi çağrısı etkisiz kalır."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except Exception:
        pass  # zaten ayarlı ya da shcore yok: yok say


class Grabber:
    """mss örneği iş parçacığına bağlıdır: onu kullanacak iş parçacığında oluştur."""

    def __init__(self):
        self._sct = (getattr(mss, "MSS", None) or mss.mss)()  # 10.x'te mss.mss() kullanımdan kalkmış
        # shortcut: monitör geometrisi Grabber ömründe bir kez okunur, çözünürlük oturum ortasında değişirse yeniden bağlanana dek kadraj bozuk, upgrade: boyut değişince Grabber'ı yeniden kur
        self._mon = getattr(self._sct, "primary_monitor", None) or self._sct.monitors[1]  # fare de birincil monitöre eşlenir

    def grab(self):
        """(RGB PIL görüntüsü, ham tampon): iki ham tamponun eşitliği 'ekran değişmedi' demektir."""
        shot = self._sct.grab(self._mon)
        return Image.frombytes("RGB", shot.size, shot.raw, "raw", "BGRX"), shot.raw

    def close(self):
        self._sct.close()  # GDI tutamaçlarını bırakır


def to_jpeg(img, quality: int, max_w: int = 1280) -> bytes:
    if img.width > max_w:
        # reducing_gap=1.0: tam sayı katında önce reduce(), 2560->1280 için 12 ms yerine 3 ms
        img = img.resize((max_w, max(1, round(img.height * max_w / img.width))),
                         Image.Resampling.BILINEAR, reducing_gap=1.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def screen_locked() -> bool:
    """Kilit ekranı / UAC güvenli masaüstünde OpenInputDesktop erişim reddiyle NULL döner."""
    try:
        desk = _user32.OpenInputDesktop(0, False, GENERIC_READ)
        if not desk:
            return True
        _user32.CloseDesktop(desk)
        return False
    except Exception:
        return False


def keep_awake(on: bool) -> None:
    """Durum iş parçacığına bağlıdır: aynı uzun ömürlü iş parçacığından aç ve kapat."""
    _kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def _kbd(vk, scan, flags):
    return INPUT(INPUT_KEYBOARD, _InputUnion(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(INPUT_MOUSE, _InputUnion(mi=MOUSEINPUT(dx, dy, data, flags, 0, 0)))


def _abs(v):
    return int(min(max(v, 0.0), 1.0) * 65535)  # NaN burada ValueError verir: reddedilir


def _send(inputs):
    """SendInput'a giden tek yer (testlerde yerine konur)."""
    arr = (INPUT * len(inputs))(*inputs)
    n = _user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        # UIPI/kilit ekranı engeli çoğu zaman hata kodu bırakmadan 0 döner
        raise OSError(f"SendInput {n}/{len(inputs)} olay işledi (UIPI veya kilitli ekran olabilir, hata {ctypes.get_last_error()})")


# shortcut: yazı Unicode olarak gönderilir (düzenden bağımsız), ham tuş kodu okuyan uygulamalar (oyun, yalnız kısayol arayüzü) harf tuşlarını görmez, upgrade: VkKeyScanEx ile karma
# shortcut: UAC/güvenli masaüstü/yetkili pencere denetlenemez (UIPI), upgrade: Host'u yönetici olarak çalıştır
# shortcut: yalnız birincil monitör, upgrade: MOUSEEVENTF_VIRTUALDESK ve monitör seçimi
class Injector:
    def __init__(self):
        self._keys = set()     # basılı VK'ler
        self._buttons = set()  # basılı fare düğmeleri

    def move(self, nx, ny):
        _send([_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, _abs(nx), _abs(ny))])

    def button(self, b, down, nx=None, ny=None):
        if b not in _BUTTONS:
            raise ValueError(f"bilinmeyen düğme: {b!r}")
        inputs = []
        if nx is not None and ny is not None:  # koordinat varsa önce oraya git, aynı çağrıda
            inputs.append(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, _abs(nx), _abs(ny)))
        inputs.append(_mouse(_BUTTONS[b][0 if down else 1]))
        if down:
            self._buttons.add(b)  # gönderim yarıda kalsa bile bırakma listesinde kalsın
        _send(inputs)
        if not down:
            self._buttons.discard(b)

    def wheel(self, delta):
        if type(delta) is not int:
            raise ValueError("tekerlek değeri tamsayı olmalı")
        d = max(-1200, min(1200, delta))
        if d:
            _send([_mouse(MOUSEEVENTF_WHEEL, data=d & 0xFFFFFFFF)])  # DWORD alanı için ikiye tümleyen

    def char(self, ch):
        if not isinstance(ch, str) or len(ch) != 1:
            raise ValueError("tek karakter bekleniyor")
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return  # denetim karakterleri yazılmaz (Enter/Tab/BackSpace 'k' olayıyla gelir)
        data = ch.encode("utf-16-le")  # tek başına vekil (surrogate) UnicodeEncodeError (ValueError) verir: reddedilir
        inputs = []
        for i in range(0, len(data), 2):  # UTF-16 kod birimi başına aşağı+yukarı
            unit = int.from_bytes(data[i:i + 2], "little")
            inputs += [_kbd(0, unit, KEYEVENTF_UNICODE), _kbd(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        _send(inputs)

    def key(self, vk, down):
        if type(vk) is not int or not 1 <= vk <= 254:
            raise ValueError(f"geçersiz sanal tuş kodu: {vk!r}")
        flags = (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0) | (0 if down else KEYEVENTF_KEYUP)
        if down:
            self._keys.add(vk)  # gönderim yarıda kalsa bile bırakma listesinde kalsın
        _send([_kbd(vk, _user32.MapVirtualKeyW(vk, 0), flags)])
        if not down:
            self._keys.discard(vk)

    def release_all(self):
        """Oturum bitince çağrılır; her bırakma ayrı try/except: biri patlasa da kalanlar bırakılsın."""
        for b in list(self._buttons):
            try:
                self.button(b, False)
            except Exception:
                pass  # başarısız olan listede kalır, sonraki çağrı yeniden dener
        for vk in list(self._keys):
            try:
                self.key(vk, False)
            except Exception:
                pass
