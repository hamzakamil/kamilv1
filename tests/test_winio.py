"""winio testleri. Girdi yapıları sahte _send ile birebir doğrulanır; gerçek SendInput yalnız bir kez,
imleci bulunduğu yere taşımak için çağrılır (tık, tuş, tekerlek hiçbir testte gerçekten gönderilmez)."""
import ctypes
import io
import subprocess
import sys
import threading
import types
from ctypes import wintypes as wt
from pathlib import Path

import mss
import pytest
from PIL import Image

from uzakmasa import winio

ROOT = Path(__file__).resolve().parents[1]

# Win32 sabitleri bilerek winio'dan alınmaz: yazım hatası testte yakalansın
KUP, EXT, UNI = 0x0002, 0x0001, 0x0004
MOVE_ABS, WHEEL = 0x0001 | 0x8000, 0x0800
EXTENDED_VKS = [0x03, *range(0x21, 0x29), 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x5F, 0x6F, 0x90, 0xA3, 0xA5]

_u32 = ctypes.WinDLL("user32")
_u32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_u32.MapVirtualKeyW.restype = wt.UINT
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_u32.GetSystemMetrics.argtypes = [ctypes.c_int]


def K(vk, scan, flags):
    return ("k", vk, scan, flags, 0, 0)


def M(flags, dx=0, dy=0, data=0):
    return ("m", dx, dy, data, flags, 0, 0)


def _desc(i):
    if i.type == 1:
        k = i.ki
        return ("k", k.wVk, k.wScan, k.dwFlags, k.time, k.dwExtraInfo)
    m = i.mi
    return ("m", m.dx, m.dy, m.mouseData, m.dwFlags, m.time, m.dwExtraInfo)


@pytest.fixture
def sent(monkeypatch):
    """Her _send çağrısı bir liste olarak kaydedilir; hiçbir şey sisteme gitmez."""
    calls = []
    monkeypatch.setattr(winio, "_send", lambda inputs: calls.append([_desc(i) for i in inputs]))
    return calls


# --- yapılar ve SendInput çağrısı ---

def test_struct_layout_matches_win32():
    assert ctypes.sizeof(winio.INPUT) == 40
    assert ctypes.sizeof(winio.MOUSEINPUT) == 32
    assert ctypes.sizeof(winio.KEYBDINPUT) == 24
    assert ctypes.sizeof(winio.ULONG_PTR) == ctypes.sizeof(ctypes.c_void_p)
    assert winio.INPUT.mi.offset == winio.INPUT.ki.offset == 8
    assert winio.MOUSEINPUT.dwExtraInfo.offset == 24 and winio.KEYBDINPUT.dwExtraInfo.offset == 16


def test_send_passes_array_and_size_to_sendinput(monkeypatch):
    got = []

    def fake(n, arr, size):
        got.append((n, size, [(arr[j].type, arr[j].ki.wScan) for j in range(n)]))
        return n

    monkeypatch.setattr(winio, "_user32", types.SimpleNamespace(SendInput=fake))
    winio.Injector().char("ş")
    assert got == [(2, 40, [(1, 0x15F), (1, 0x15F)])]


@pytest.mark.parametrize("accepted", [0, 1])
def test_send_raises_when_system_blocks_input(monkeypatch, accepted):
    monkeypatch.setattr(winio, "_user32", types.SimpleNamespace(SendInput=lambda n, arr, size: accepted))
    with pytest.raises(OSError):
        winio.Injector().button("l", True, 0.5, 0.5)  # iki olay: 0 ya da 1 işlenmesi hata sayılır


# --- char ---

def test_char_turkish_is_unicode_down_up(sent):
    winio.Injector().char("ş")
    assert sent == [[K(0, 0x15F, UNI), K(0, 0x15F, UNI | KUP)]]  # tek SendInput çağrısı


@pytest.mark.parametrize("ch", ["a", " ", "@", "€", "İ", "ı", "~"])
def test_char_scan_is_code_unit(sent, ch):
    winio.Injector().char(ch)
    assert sent == [[K(0, ord(ch), UNI), K(0, ord(ch), UNI | KUP)]]


def test_char_astral_sends_surrogate_pair(sent):
    winio.Injector().char("\U0001F600")  # D83D DE00
    assert sent == [[K(0, 0xD83D, UNI), K(0, 0xD83D, UNI | KUP), K(0, 0xDE00, UNI), K(0, 0xDE00, UNI | KUP)]]


@pytest.mark.parametrize("ch", [chr(c) for c in range(0x20)] + ["\x7f"])
def test_char_control_is_ignored(sent, ch):
    winio.Injector().char(ch)
    assert sent == []


@pytest.mark.parametrize("ch", ["", "ab", "\ud83d", 65, None])
def test_char_rejects_non_scalar(sent, ch):
    with pytest.raises(ValueError):
        winio.Injector().char(ch)
    assert sent == []


# --- key ---

@pytest.mark.parametrize("vk", EXTENDED_VKS, ids=hex)
def test_key_extended_flag_and_scan(sent, vk):
    inj = winio.Injector()
    inj.key(vk, True)
    inj.key(vk, False)
    scan = _u32.MapVirtualKeyW(vk, 0)
    assert sent == [[K(vk, scan, EXT)], [K(vk, scan, EXT | KUP)]]


@pytest.mark.parametrize("vk", [0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x1B, 0x20, 0x41, 0x5A, 0x70, 0x7B, 0xA0, 0xA2, 0xA4], ids=hex)
def test_key_plain_has_no_extended_flag(sent, vk):
    inj = winio.Injector()
    inj.key(vk, True)
    inj.key(vk, False)
    scan = _u32.MapVirtualKeyW(vk, 0)
    assert sent == [[K(vk, scan, 0)], [K(vk, scan, KUP)]]


@pytest.mark.parametrize("vk,scan", [(0x0D, 0x1C), (0x1B, 0x01), (0x20, 0x39), (0x08, 0x0E), (0x09, 0x0F), (0x70, 0x3B)])
def test_key_scan_code_is_mapvirtualkey(sent, vk, scan):
    winio.Injector().key(vk, True)
    assert sent == [[K(vk, scan, 0)]]  # bu tuşların tarama kodu klavye düzenine bağlı değil


def test_key_arrow_left_example(sent):
    winio.Injector().key(0x25, True)
    assert sent == [[K(0x25, _u32.MapVirtualKeyW(0x25, 0), EXT)]]


@pytest.mark.parametrize("vk", [1, 254])
def test_key_accepts_range_edges(sent, vk):
    winio.Injector().key(vk, True)
    assert len(sent) == 1


@pytest.mark.parametrize("vk", [0, -1, 255, 256, 10**6, 65.0, True, "65", None])
def test_key_rejects_bad_vk(sent, vk):
    with pytest.raises(ValueError):
        winio.Injector().key(vk, True)
    assert sent == []


# --- move / button / wheel ---

@pytest.mark.parametrize("nx,ny,dx,dy", [
    (0.5, 0.5, 32767, 32767),
    (0, 0, 0, 0),
    (1, 1, 65535, 65535),
    (0.25, 0.75, 16383, 49151),
    (-0.5, 2, 0, 65535),            # sınırlama
    (1e9, -1e9, 65535, 0),
    (float("inf"), float("-inf"), 65535, 0),
])
def test_move_is_absolute_and_clamped(sent, nx, ny, dx, dy):
    winio.Injector().move(nx, ny)
    assert sent == [[M(MOVE_ABS, dx, dy)]]


def test_move_rejects_nan(sent):
    with pytest.raises(ValueError):
        winio.Injector().move(float("nan"), 0.5)
    assert sent == []


@pytest.mark.parametrize("b,down,up", [("l", 0x0002, 0x0004), ("r", 0x0008, 0x0010), ("m", 0x0020, 0x0040)])
def test_button_flags_without_coordinates(sent, b, down, up):
    inj = winio.Injector()
    inj.button(b, True)
    inj.button(b, False)
    assert sent == [[M(down)], [M(up)]]


def test_button_with_coordinates_moves_first_in_same_call(sent):
    inj = winio.Injector()
    inj.button("l", True, 0.25, 0.75)
    inj.button("r", False, 1, 0)
    assert sent == [[M(MOVE_ABS, 16383, 49151), M(0x0002)], [M(MOVE_ABS, 65535, 0), M(0x0010)]]


def test_button_rejects_unknown(sent):
    with pytest.raises(ValueError):
        winio.Injector().button("x", True)
    assert sent == []


@pytest.mark.parametrize("delta,data", [
    (120, 120), (-120, 0xFFFFFF88),  # DWORD alanı: ikiye tümleyen
    (8, 8), (-8, 0xFFFFFFF8),        # dokunmatik yüzey: 120'nin katı olmayan değerler
    (1200, 1200), (-1200, 0xFFFFFB50),
    (5000, 1200), (-5000, 0xFFFFFB50),  # sınırlama
])
def test_wheel(sent, delta, data):
    winio.Injector().wheel(delta)
    assert sent == [[M(WHEEL, 0, 0, data)]]


def test_wheel_zero_sends_nothing_and_non_int_rejected(sent):
    inj = winio.Injector()
    inj.wheel(0)
    assert sent == []
    for bad in (1.5, True, "120", None):
        with pytest.raises(ValueError):
            inj.wheel(bad)
    assert sent == []


# --- release_all ---

def test_release_all_releases_exactly_what_is_down(sent):
    inj = winio.Injector()
    inj.key(0x11, True)
    inj.key(0x10, True)
    inj.key(0x41, True)
    inj.key(0x41, False)
    inj.button("l", True)
    inj.button("r", True)
    inj.button("r", False)
    sent.clear()
    inj.release_all()
    flat = [d for call in sent for d in call]
    assert sorted(flat) == sorted([M(0x0004), K(0x10, _u32.MapVirtualKeyW(0x10, 0), KUP), K(0x11, _u32.MapVirtualKeyW(0x11, 0), KUP)])
    sent.clear()
    inj.release_all()
    assert sent == []  # ikinci çağrıda bırakılacak bir şey kalmadı


@pytest.mark.parametrize("failing", ["l", 0x10, 0x11])  # düğme ya da iki tuştan biri patlasın
def test_release_all_survives_failed_release_and_retries_it(monkeypatch, failing):
    inj = winio.Injector()
    monkeypatch.setattr(winio, "_send", lambda inputs: None)
    inj.key(0x10, True)
    inj.key(0x11, True)
    inj.button("l", True)
    calls, failed = [], []

    def flaky(inputs):
        d = _desc(inputs[0])
        calls.append([d])
        if d[0] == "m" if failing == "l" else d[1] == failing:  # basılı düğme tek fare olayıdır
            failed.append([d])
            raise OSError("engellendi")

    monkeypatch.setattr(winio, "_send", flaky)
    inj.release_all()  # fırlatmamalı
    assert len(calls) == 3 and len(failed) == 1  # üç bırakmanın üçü de denendi
    retry = []
    monkeypatch.setattr(winio, "_send", lambda inputs: retry.append([_desc(i) for i in inputs]))
    inj.release_all()
    assert retry == failed  # yalnız başarısız olan hâlâ basılı sayılıyordu


def test_failed_press_still_counts_as_down(monkeypatch):
    inj = winio.Injector()

    def boom(inputs):
        raise OSError("engellendi")

    monkeypatch.setattr(winio, "_send", boom)
    with pytest.raises(OSError):
        inj.key(0x10, True)
    retry = []
    monkeypatch.setattr(winio, "_send", lambda inputs: retry.append([_desc(i) for i in inputs]))
    inj.release_all()
    assert retry == [[K(0x10, _u32.MapVirtualKeyW(0x10, 0), KUP)]]


# --- Grabber / to_jpeg ---

class _Shot:
    def __init__(self, raw, size):
        self.raw, self.size = raw, size


def test_grabber_converts_bgrx_to_rgb_and_returns_raw_buffer(monkeypatch):
    raw = bytearray([1, 2, 3, 0, 10, 20, 30, 255])  # B G R X, B G R X
    grabbed = []

    class FakeMSS:
        monitors = [{"left": 0, "top": 0, "width": 99, "height": 99}, {"left": 0, "top": 0, "width": 2, "height": 1}]

        def grab(self, mon):
            grabbed.append(mon)
            return _Shot(raw, (2, 1))

        def close(self):
            grabbed.append("closed")

    monkeypatch.setattr(winio.mss, "MSS", FakeMSS)
    g = winio.Grabber()
    img, key = g.grab()
    g.close()
    assert img.mode == "RGB" and img.size == (2, 1)
    assert img.getpixel((0, 0)) == (3, 2, 1) and img.getpixel((1, 0)) == (30, 20, 10)
    assert key == raw
    assert grabbed == [FakeMSS.monitors[1], "closed"]  # primary_monitor yoksa monitors[1]


def test_grabber_prefers_primary_monitor(monkeypatch):
    primary = {"left": 0, "top": 0, "width": 2, "height": 1}
    grabbed = []

    class FakeMSS:
        monitors = [{}, {"left": 2560, "top": 0, "width": 9, "height": 9}, primary]
        primary_monitor = primary

        def grab(self, mon):
            grabbed.append(mon)
            return _Shot(bytearray(8), (2, 1))

    monkeypatch.setattr(winio.mss, "MSS", FakeMSS)
    winio.Grabber().grab()
    assert grabbed == [primary]


def test_grabber_real_capture_size_and_types():
    winio.set_dpi_aware()
    if winio.screen_locked():
        pytest.skip("ekran kilitli")
    with mss.MSS() as sct:
        mons = sct.monitors[1:]
    mon = next((m for m in mons if m.get("is_primary")), mons[0])
    g = winio.Grabber()
    try:
        img, key = g.grab()
        _, key2 = g.grab()
    finally:
        g.close()
    assert isinstance(img, Image.Image) and img.mode == "RGB"
    assert img.size == (mon["width"], mon["height"])
    assert isinstance(key, (bytes, bytearray)) and len(key) == img.width * img.height * 4
    assert key is not key2  # her çekim yeni tampon: 'ekran değişmedi' eşitliği buna dayanır
    assert any(hi > 0 for _, hi in img.getextrema())  # görüntü bakılmaz, yalnız tamamen siyah olmadığı doğrulanır


def _gradient(w, h):
    return Image.linear_gradient("L").resize((w, h)).convert("RGB")


def test_to_jpeg_resizes_and_is_valid_jpeg():
    data = winio.to_jpeg(_gradient(2560, 1440), 55)
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    out = Image.open(io.BytesIO(data))
    assert out.format == "JPEG" and out.size == (1280, 720)


def test_to_jpeg_respects_max_w_and_never_upscales():
    assert Image.open(io.BytesIO(winio.to_jpeg(_gradient(2560, 1440), 55, 640))).size == (640, 360)
    assert Image.open(io.BytesIO(winio.to_jpeg(_gradient(3440, 1440), 55))).size == (1280, 536)
    assert Image.open(io.BytesIO(winio.to_jpeg(_gradient(800, 600), 55))).size == (800, 600)
    assert Image.open(io.BytesIO(winio.to_jpeg(_gradient(5000, 1), 55))).size == (1280, 1)  # yükseklik 0'a düşmez


def test_to_jpeg_quality_changes_size():
    img = _gradient(1280, 720)
    assert len(winio.to_jpeg(img, 30)) < len(winio.to_jpeg(img, 90))


# --- screen_locked / keep_awake / set_dpi_aware ---

@pytest.mark.parametrize("handle,locked", [(0, True), (None, True), (1234, False)])
def test_screen_locked_follows_open_input_desktop(monkeypatch, handle, locked):
    calls, closed = [], []
    fake = types.SimpleNamespace(
        OpenInputDesktop=lambda flags, inherit, access: calls.append((flags, inherit, access)) or handle,
        CloseDesktop=closed.append,
    )
    monkeypatch.setattr(winio, "_user32", fake)
    assert winio.screen_locked() is locked
    assert calls == [(0, False, 0x80000000)]
    assert closed == ([] if locked else [1234])  # tutamaç yalnız başarıda kapatılır


def test_screen_locked_error_means_not_locked(monkeypatch):
    def boom(*a):
        raise OSError("hata")

    monkeypatch.setattr(winio, "_user32", types.SimpleNamespace(OpenInputDesktop=boom))
    assert winio.screen_locked() is False


def test_screen_locked_real_returns_bool():
    assert isinstance(winio.screen_locked(), bool)


def test_keep_awake_round_trip():
    k32 = ctypes.WinDLL("kernel32")
    k32.SetThreadExecutionState.argtypes = [wt.DWORD]
    k32.SetThreadExecutionState.restype = wt.DWORD
    out = {}

    def run():  # durum iş parçacığına bağlı: ayrı iş parçacığında dene, ana iş parçacığı etkilenmesin
        try:
            winio.keep_awake(True)
            out["on"] = k32.SetThreadExecutionState(0x80000001)   # aynı değer: önceki durumu okur, değiştirmez
            winio.keep_awake(False)
            out["off"] = k32.SetThreadExecutionState(0x80000000)  # aynı değer: önceki durumu okur, değiştirmez
        except BaseException as e:
            out["err"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert "err" not in out
    assert out["on"] == 0x80000001   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED kuruldu
    assert out["off"] == 0x80000000  # yalnız ES_CONTINUOUS: uyku engeli kalktı


def test_set_dpi_aware_is_idempotent_and_system_aware():
    code = (
        "import ctypes\n"
        "from uzakmasa import winio\n"
        "winio.set_dpi_aware(); winio.set_dpi_aware()\n"
        "a = ctypes.c_int(-1)\n"
        "hr = ctypes.WinDLL('shcore').GetProcessDpiAwareness(None, ctypes.byref(a))\n"
        "print(hr, a.value)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0", "1"]  # S_OK, sistem DPI farkında (ayrı süreç: başka testler karışmaz)


# --- tek gerçek SendInput ---

def test_real_move_to_current_position_leaves_cursor_unchanged():
    winio.set_dpi_aware()
    if winio.screen_locked():
        pytest.skip("ekran kilitli")
    w, h = _u32.GetSystemMetrics(0), _u32.GetSystemMetrics(1)
    inj = winio.Injector()

    def cursor():
        p = wt.POINT()
        _u32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    # kullanıcı fareyi tam o anda oynatırsa yanlış alarm olmasın: birkaç denemeden biri tutarsa yeter
    # (gerçek bir kayma hatası her denemede imleci oynatır)
    for _ in range(5):
        x, y = cursor()
        if not (0 <= x < w and 0 <= y < h):
            pytest.skip("imleç birincil monitörde değil")
        inj.move((x + 0.5) / w, (y + 0.5) / h)  # piksel merkezi: tam sayıya yuvarlanınca aynı piksel
        if cursor() == (x, y):
            return
    pytest.fail("mutlak hareket imleci bulunduğu pikselden oynattı")
