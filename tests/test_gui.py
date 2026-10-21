"""gui.py testleri: gerçek Tk kökü (her testte kurulur, sonunda yıkılır), sahte motorlar, sentetik Tk olayları.

Gerçek girdi YOK: event_generate yalnızca Tk'nin kendi olay kuyruğuna sentetik olay koyar.
Pencereler UZAK_SHOTS ayarlı değilse tamamen saydamdır (-alpha 0) ve kısa ömürlüdür. UZAK_SHOTS=<klasör> ile
çalıştırılırsa test_screenshots yalnızca KENDİ pencerelerinin sınırlayıcı kutusunu oraya PNG olarak yazar.
"""
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk

from uzakmasa import config, gui, proto

SHOTS = os.environ.get("UZAK_SHOTS")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "2abcDEF456ghiJKL789mnoPQR012stuVWX345yz"  # sahte


# --- sahte motorlar (Host/Client API'si: SPEC §5, §6) ---

class StubHost:
    made = []

    def __init__(self, cfg, ui, mode, save=lambda cfg: None, **kw):
        self.cfg, self.ui, self.mode, self.save, self.calls = cfg, ui, mode, save, []
        StubHost.made.append(self)

    def start(self): self.calls.append("start")
    def stop(self): self.calls.append("stop")
    def kick(self): self.calls.append("kick")


class StubClient:
    made = []

    def __init__(self, conn_id, password, ui, uid, relay=None):
        self.args = (conn_id, password, uid, relay)
        self.ui, self.sent, self.calls = ui, [], []
        StubClient.made.append(self)

    def start(self): self.calls.append("start")
    def stop(self): self.calls.append("stop")
    def send_input(self, ev): self.sent.append(ev)


# --- Tk altyapısı ---

class SpyTk:
    """root.tk'nin sarmalayıcısı: Tk'ye ana iş parçacığı dışından yapılan HER çağrıyı kaydeder (iş parçacığı kuralı).
    Variable/PhotoImage __del__'i de buna dahildir: motor iş parçacığının Tk nesnesine son referansı bırakması da ihlaldir."""

    def __init__(self, real, bad):
        self._real, self._bad, self._main = real, bad, threading.get_ident()

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **k):
            if threading.get_ident() != self._main:
                self._bad.append((name, a[:3], threading.current_thread().name))
            return attr(*a, **k)
        return wrapped


class AlphaTop(tk.Toplevel):
    """Testlerde kullanıcının masaüstünü rahatsız etmeyen saydam üst düzey pencere."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.attributes("-alpha", 0.0)


def new_root():
    """tk.Tk(); eşzamanlı süreçler (başka testler, virüs tarayıcı) Tcl kitaplığını anlık okunamaz yapabiliyor: birkaç kez dene."""
    for attempt in range(6):
        try:
            return tk.Tk()
        except tk.TclError as e:
            if not any(s in str(e) for s in ("usable", "tcl_findLibrary")) or attempt == 5:
                raise
            time.sleep(0.5)


@pytest.fixture(autouse=True)
def appdir(tmp_path, monkeypatch):
    """Gerçek profile asla dokunma: APPDIR tmp_path'e yönlenir."""
    monkeypatch.setattr(config, "APPDIR", str(tmp_path / "Kamil V1"))
    StubHost.made.clear()
    StubClient.made.clear()
    return tmp_path / "Kamil V1"


@pytest.fixture
def make_app(monkeypatch):
    """Her çağrı yeni bir Tk kökü + App döndürür. Test bitince hepsi yıkılır ve iş parçacığı kuralı doğrulanır."""
    made, bad = [], []
    if not SHOTS:
        monkeypatch.setattr(tk, "Toplevel", AlphaTop)

    def factory(host_cls=StubHost, client_cls=StubClient, cfg=None):
        if cfg is not None:
            config.save(cfg)
        root = new_root()
        root.tk = SpyTk(root.tk, bad)  # App pencere öğelerini kurmadan önce
        if not SHOTS:
            root.attributes("-alpha", 0.0)
        app = gui.App(root, host_cls, client_cls)
        made.append(app)
        root.update()  # pencere öğeleri eşlensin
        return app

    yield factory
    for app in made:
        for fn in (app.close, app.root.destroy):
            try:
                fn()
            except tk.TclError:
                pass  # test zaten kapattı
    assert not bad, f"Tk'ye ana iş parçacığı dışından dokunuldu: {bad}"


def pump(root, cond, timeout=3.0):
    """Tk olaylarını (after kuyruğu dahil) cond doğru olana kadar işler."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.005)
    root.update()
    return bool(cond())


def run_thread(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def descendants(w):
    for c in w.winfo_children():
        yield c
        yield from descendants(c)


def texts(w):
    out = []
    for c in descendants(w):
        try:
            t = c.cget("text")
        except tk.TclError:
            continue
        if not t:
            try:
                var = str(c.cget("textvariable"))
                t = c.getvar(var) if var else ""
            except tk.TclError:
                t = ""
        if t:
            out.append(str(t))
    return out


def layout_problems(top):
    """Pencere dışına taşan öğeler ve kendilerine ayrılan genişliğe sığmayan düğme/etiket yazıları."""
    top.update()
    st = ttk.Style(top)
    x0, y0 = top.winfo_rootx(), top.winfo_rooty()
    x1, y1 = x0 + top.winfo_width(), y0 + top.winfo_height()
    bad = []
    for w in descendants(top):
        if not w.winfo_ismapped():
            continue
        if w.winfo_rootx() < x0 or w.winfo_rooty() < y0 or w.winfo_rootx() + w.winfo_width() > x1 \
                or w.winfo_rooty() + w.winfo_height() > y1:
            bad.append(f"pencere dışına taşıyor: {w}")
        cls = w.winfo_class()
        if cls not in ("TButton", "TLabel", "TRadiobutton"):
            continue
        text = w.cget("text") or (w.getvar(str(w.cget("textvariable"))) if str(w.cget("textvariable")) else "")
        if not text or (cls == "TLabel" and int(float(str(w.cget("wraplength")) or 0)) > 0):
            continue  # sarılan etiketler sığar (yükseklik büyür)
        own = str(w.cget("font")) if cls == "TLabel" else ""  # ttk düğmesinde -font yok: yazı tipi stilden gelir
        font = tkfont.Font(root=top, font=own or st.lookup(str(w.cget("style")) or cls, "font") or "TkDefaultFont")
        need = max(font.measure(line) for line in str(text).split("\n"))
        if need > w.winfo_width():
            bad.append(f"yazı sığmıyor ({need} > {w.winfo_width()} px): {text!r}")
    return bad


def make_frame(w=1280, h=720, color=(30, 90, 160)):
    img = Image.new("RGB", (w, h), color)
    img.paste((240, 200, 40), (0, 0, w, h // 10))  # üst şerit: ölçeklenen karede konum doğrulanabilsin
    return img


def pixel(viewer, x, y):
    """Tk'nin çizdiği PhotoImage'tan piksel oku."""
    return tuple(viewer._photo._PhotoImage__photo.get(x, y))


def start_host(app, mode="ngrok"):
    hp = app.host_panel
    app.show(hp)
    hp.mode.set(mode)
    if mode == "ngrok":
        hp.token.set(TOKEN)
    else:
        hp.relay.set("vps.example.com:9000")
        hp.relay_token.set("gizli-anahtar")
    hp._toggle()
    return hp, StubHost.made[-1]


def open_viewer(app, size=(1280, 720), relay=None):
    """Client panelinden sahte bir Client ile bağlanır, ilk kareyi yollar, görüntüleyici görünene kadar bekler."""
    cp = app.client_panel
    app.show(cp)
    cp.conn.set("0.tcp.eu.ngrok.io:12345")
    cp.pw.set("ABCD2345EFGH")
    cp._toggle()
    client = StubClient.made[-1]
    run_thread(client.ui.frame, make_frame(*size)).join()
    assert pump(app.root, lambda: cp.viewer is not None and cp.viewer.shown)
    return cp, client, cp.viewer


def settle(app, viewer, geometry):
    """Görüntüleyiciyi verilen boyuta getir ve yeniden sığdırma bitene kadar bekle; tuval boyutunu döndür."""
    viewer.top.geometry(geometry)
    assert pump(app.root, lambda: viewer._rect is not None and not viewer._refit)
    pump(app.root, lambda: False, 0.15)  # son Configure/yeniden sığdırma
    return viewer.canvas.winfo_width(), viewer.canvas.winfo_height()


# --- açılış ekranı ve gezinti ---

def test_launcher_has_the_two_exact_buttons_and_swaps_panels(make_app):
    app = make_app()
    root = app.root
    assert root.title() == "Kamil V1"
    assert app.launcher.host_btn.cget("text") == "Bu Bilgisayarın İzlenmesine İzin Ver (Sunucu/Host)"
    assert app.launcher.client_btn.cget("text") == "Uzaktaki Bilgisayarı Yönet (İstemci/Client)"
    assert app.launcher.winfo_ismapped() and not app.host_panel.winfo_ismapped()
    app.launcher.host_btn.invoke()
    root.update()
    assert app.host_panel.winfo_ismapped() and not app.launcher.winfo_ismapped()
    app.host_panel.back.invoke()
    app.launcher.client_btn.invoke()
    root.update()
    assert app.client_panel.winfo_ismapped() and not app.host_panel.winfo_ismapped()
    app.client_panel.back.invoke()
    root.update()
    assert app.launcher.winfo_ismapped()


def test_back_is_disabled_while_host_runs(make_app):
    app = make_app()
    hp, host = start_host(app)
    assert hp.back.instate(["disabled"])  # Host penceresi arkada gizli kalmasın (gizlilik yok ilkesi)
    hp.stop()
    assert not hp.back.instate(["disabled"])


@pytest.mark.parametrize("screen", ["launcher", "host_panel", "client_panel"])
def test_panels_are_not_clipped(make_app, screen):
    app = make_app()
    app.show(getattr(app, screen))
    app.root.update()
    assert layout_problems(app.root) == []


def test_host_relay_mode_layout_is_not_clipped(make_app):
    app = make_app()
    app.show(app.host_panel)
    app.host_panel.mode.set("relay")
    app.root.update()
    assert layout_problems(app.root) == []


# --- Host paneli: Başlat kuralları, şifre politikası ---

def test_start_disabled_without_token_in_ngrok_mode(make_app):
    hp = make_app().host_panel
    assert hp.mode.get() == "ngrok" and hp.token.get() == config.DEFAULT_NGROK_TOKEN == ""
    assert hp.btn.cget("text") == "Hazır Ol / Başlat" and hp.btn.instate(["disabled"])
    assert "Authtoken" in hp.hint.cget("text")
    hp.token.set("   ")
    assert hp.btn.instate(["disabled"])  # yalnızca boşluk token sayılmaz
    hp.token.set(TOKEN)
    assert not hp.btn.instate(["disabled"]) and hp.hint.cget("text") == ""
    hp.token.set("")
    assert hp.btn.instate(["disabled"])


def test_baked_in_default_token_enables_start(make_app, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_NGROK_TOKEN", TOKEN)
    hp = make_app().host_panel
    assert hp.token.get() == TOKEN and not hp.btn.instate(["disabled"])


def test_relay_mode_needs_valid_address_and_token(make_app):
    hp = make_app().host_panel
    hp.mode.set("relay")
    assert hp.btn.instate(["disabled"])
    hp.relay.set("vps.example.com")  # port yok
    hp.relay_token.set("t")
    assert hp.btn.instate(["disabled"])
    hp.relay.set("vps.example.com:99999")
    assert hp.btn.instate(["disabled"])
    hp.relay.set("vps.example.com:9000")
    assert not hp.btn.instate(["disabled"])
    hp.relay_token.set("")
    assert hp.btn.instate(["disabled"])
    assert hp.ngrok_box.winfo_manager() == "" and hp.relay_box.winfo_manager() == "grid"


def test_parse_relay():
    assert gui.parse_relay(" vps.example.com:9000 ") == ("vps.example.com", 9000)
    assert gui.parse_relay("1.2.3.4:1") == ("1.2.3.4", 1)
    for bad in ("", "vps", ":9000", "vps:", "vps:0", "vps:65536", "vps:abc", "vps:²"):
        assert gui.parse_relay(bad) is None, bad


def test_generated_password_has_12_symbols_from_the_31_symbol_alphabet():
    assert len(gui.ALPHABET) == 31 == len(set(gui.ALPHABET))
    assert not set("0O1IL") & set(gui.ALPHABET)
    seen = set()
    for _ in range(300):
        pw = gui.new_password()
        assert len(pw) == 12 and set(pw) <= set(gui.ALPHABET)
        seen.add(pw)
    assert len(seen) == 300  # 59 bit: çakışma yok


def test_panel_starts_with_generated_password_and_regenerate_changes_it(make_app):
    hp = make_app().host_panel
    first = hp.pw.get()
    assert len(first) == 12 and set(first) <= set(gui.ALPHABET) and hp.pw_entry.instate(["!disabled"])
    hp.regen.invoke()
    assert hp.pw.get() != first and len(hp.pw.get()) == 12


@pytest.mark.parametrize("length,ok", [(0, False), (8, False), (15, False), (16, True), (30, True)])
def test_custom_password_under_16_is_rejected(make_app, length, ok):
    hp = make_app().host_panel
    hp.token.set(TOKEN)
    hp.pw.set("x" * length)
    assert hp.btn.instate(["disabled"]) is (not ok)
    assert "daha güvenli" in hp.pw_note.cget("text")  # her özel şifrede satır içi uyarı
    assert (f"en az {gui.MIN_CUSTOM_PW} karakter" in hp.pw_note.cget("text")) is (not ok)
    assert (str(hp.pw_note.cget("style")) == "Bad.TLabel") is (not ok)
    hp._toggle()  # düğme kapalı olsa da koruma çalışmalı
    assert bool(StubHost.made) is ok


def test_password_note_changes_with_kind(make_app):
    hp = make_app().host_panel
    gen = hp.pw.get()
    assert "önerilen" in hp.pw_note.cget("text")
    hp.pw.set("kisa")
    assert "en az 16" in hp.pw_note.cget("text") and "4/16" in hp.pw_note.cget("text")
    hp.pw.set("bu-cok-uzun-ozel-bir-sifre-123")
    assert "daha güvenli" in hp.pw_note.cget("text")
    hp.pw.set(gen)  # üretilen şifreye dönmek uyarıyı kaldırır
    assert "önerilen" in hp.pw_note.cget("text") and str(hp.pw_note.cget("style")) == "Hint.TLabel"


def test_start_derives_key_saves_config_and_hands_cfg_to_host(make_app, appdir):
    app = make_app()
    hp, host = start_host(app)
    pw = hp.pw.get()
    assert host.calls == ["start"] and host.mode == "ngrok"
    assert host.cfg["k"] == proto.derive_key(pw).hex() and host.cfg["ngrok_token"] == TOKEN
    assert host.save is config.save
    saved = config.load()
    assert saved["k"] == host.cfg["k"] and saved["ngrok_token"] == TOKEN and saved["mode"] == "ngrok"
    assert pw not in (appdir / "config.json").read_text(encoding="utf-8")  # şifrenin kendisi asla diskte değil
    assert hp.btn.cget("text") == "Durdur" and not hp.btn.instate(["disabled"])
    assert hp.token_entry.instate(["disabled"]) and hp.pw_entry.instate(["disabled"]) and hp.regen.instate(["disabled"])


def test_custom_16_char_password_is_used(make_app):
    app = make_app()
    hp = app.host_panel
    hp.token.set(TOKEN)
    hp.pw.set("  dort-kelimelik-parola  ")  # baştaki/sondaki boşluk (kopyala-yapıştır) atılır
    hp._toggle()
    assert StubHost.made[-1].cfg["k"] == proto.derive_key("dort-kelimelik-parola").hex()


def test_saved_key_is_shown_as_placeholder_and_reused_until_the_user_types(make_app):
    k = "ab" * 32
    cfg = config.load()
    cfg["k"] = k
    cfg["ngrok_token"] = TOKEN
    app = make_app(cfg=cfg)
    hp = app.host_panel
    assert hp.pw.get() == gui.SAVED_PW and not hp.btn.instate(["disabled"])
    assert "Kayıtlı" in hp.pw_note.cget("text")
    hp._toggle()
    assert StubHost.made[-1].cfg["k"] == k  # yeniden türetilmedi
    hp.stop()
    hp.pw.set("")  # yazmaya başlayınca kayıtlı şifre bırakılır
    assert hp.btn.instate(["disabled"])
    hp.regen.invoke()
    hp._toggle()
    assert StubHost.made[-1].cfg["k"] == proto.derive_key(hp.pw.get()).hex() != k


def test_saved_placeholder_is_ignored_without_a_saved_key(make_app):
    hp = make_app().host_panel
    hp.pw.set(gui.SAVED_PW)  # k yok: 15 karakterlik özel şifre sayılır, reddedilir
    assert hp.btn.instate(["disabled"])


def test_host_mode_and_credentials_prefilled_from_config(make_app):
    cfg = config.load()
    cfg.update(mode="relay", relay="vps.example.com:9000", relay_token="abc", ngrok_token=TOKEN)
    hp = make_app(cfg=cfg).host_panel
    assert hp.mode.get() == "relay" and hp.relay.get() == "vps.example.com:9000" and hp.relay_token.get() == "abc"
    assert hp.token.get() == TOKEN
    assert hp.conn.get() == gui.fmt_id(cfg["relay_id"])  # relay ID sabit: Başlat'tan önce de görünür


def test_connection_id_placeholder_and_copy(make_app):
    app = make_app()
    hp = app.host_panel
    assert hp.conn.get() == "(Başlat'a basınca oluşur)" and hp.copy.instate(["disabled"])
    hp.token.set(TOKEN)
    hp._toggle()
    assert hp.conn.get() == gui.ID_PREPARING and hp.copy.instate(["disabled"])
    run_thread(StubHost.made[-1].ui.ready, "0.tcp.eu.ngrok.io:12345").join()
    assert pump(app.root, lambda: hp.conn.get() == "0.tcp.eu.ngrok.io:12345")
    assert not hp.copy.instate(["disabled"])
    hp.copy.invoke()
    assert app.root.clipboard_get() == "0.tcp.eu.ngrok.io:12345"
    hp.stop()
    assert hp.conn.get() == gui.ID_PLACEHOLDER


def test_fmt_id():
    assert gui.fmt_id("123456789") == "123 456 789"
    assert gui.fmt_id("0.tcp.eu.ngrok.io:12345") == "0.tcp.eu.ngrok.io:12345"
    assert gui.fmt_id("12345") == "12345"


def test_first_pairing_sentence_is_shown(make_app):
    hp = make_app().host_panel
    assert any("İzin ver" in t and "İlk eşleşmede" in t for t in texts(hp))


# --- Host olayları: iş parçacığı kuralı ---

def test_host_events_reach_the_panel_only_through_the_queue(make_app):
    app = make_app()
    hp, host = start_host(app)
    ui = host.ui

    def worker():  # her çağrı motor iş parçacığından; casus Tk'ye dokunan olursa fixture teardown'da düşer
        ui.log("ngrok hazırlanıyor (ilk çalıştırmada indirilir)…")
        ui.ready("0.tcp.eu.ngrok.io:12345")
        ui.stats(5 * 1024 * 1024)
        ui.connected("Ayşe'nin Dizüstü", "88.1.2.3")

    run_thread(worker).join()
    assert hp.state.get() == "Başlatılıyor…"  # kuyruk henüz boşaltılmadı: iş parçacığı Tk'ye dokunmadı
    assert pump(app.root, lambda: hp.banner.winfo_ismapped())
    assert "Ayşe'nin Dizüstü" in hp.banner.cget("text") and "88.1.2.3" in hp.banner.cget("text")
    assert hp.conn.get() == "0.tcp.eu.ngrok.io:12345"
    assert "Gönderilen: 5 MB" in hp.info.get() and "Uyku engellendi" in hp.info.get()
    assert "ngrok hazırlanıyor" in hp.log.get()
    assert hp.state.get() == "Bağlı: Ayşe'nin Dizüstü" and not hp.kick.instate(["disabled"])
    run_thread(ui.disconnected).join()
    assert pump(app.root, lambda: not hp.banner.winfo_ismapped())
    assert hp.kick.instate(["disabled"]) and hp.state.get().startswith("Hazır")


def test_host_setup_error_returns_to_idle_with_the_message_so_the_user_can_retry(make_app):
    app = make_app()
    hp, host = start_host(app)
    msg = "ngrok Authtoken eksik ya da geçersiz: ngrok.com panelinden kopyalayın. (" + "x" * 190 + ")"
    run_thread(host.ui.error, msg).join()
    assert pump(app.root, lambda: hp.host is None)
    assert host.calls == ["start", "stop"]  # motor ana iş parçacığı zaten bitti; kalan kaynaklar kapatılır
    assert hp.state.get() == "Hata: " + msg and str(hp.state_lbl.cget("style")) == "Bad.TLabel"
    assert hp.btn.cget("text") == "Hazır Ol / Başlat" and not hp.btn.instate(["disabled"])
    assert hp.token_entry.instate(["!disabled"])
    assert layout_problems(app.root) == []  # uzun hata metni sarılır, pencere dışına taşmaz
    hp.token.set("yeni-" + TOKEN)
    hp._toggle()  # düzeltip yeniden başlatabilir
    assert len(StubHost.made) == 2 and StubHost.made[-1].calls == ["start"]


def test_long_multiline_log_lines_are_flattened_and_capped(make_app):
    app = make_app()
    hp, host = start_host(app)
    run_thread(host.ui.log, "Beklenmeyen hata:\n" + "  File x, line 1\n" * 100).join()
    assert pump(app.root, lambda: hp.log.get().startswith("Beklenmeyen hata: File x"))
    assert "\n" not in hp.log.get() and len(hp.log.get()) <= 300 and hp.log.get().endswith("…")


def test_data_counter_warning_in_ngrok_mode_only(make_app):
    app = make_app()
    hp, host = start_host(app)
    run_thread(host.ui.stats, 799 * 1024 * 1024).join()
    assert pump(app.root, lambda: "799 MB" in hp.info.get())
    assert str(hp.info_lbl.cget("style")) == "TLabel"
    run_thread(host.ui.stats, 900 * 1024 * 1024).join()
    assert pump(app.root, lambda: "900 MB" in hp.info.get())
    assert str(hp.info_lbl.cget("style")) == "Warn.TLabel" and "1 GB" in hp.info.get()
    hp.stop()
    hp2, host2 = start_host(app, "relay")
    run_thread(host2.ui.stats, 2 * 1024 ** 3).join()
    assert pump(app.root, lambda: "2.00 GB" in hp2.info.get())
    assert str(hp2.info_lbl.cget("style")) == "TLabel"  # relay'de kota yok


def test_fmt_bytes():
    assert gui.fmt_bytes(0) == "0 KB" and gui.fmt_bytes(2048) == "2 KB"
    assert gui.fmt_bytes(123 * 1024 ** 2) == "123 MB" and gui.fmt_bytes(1536 * 1024 ** 2) == "1.50 GB"


def test_stale_events_from_a_stopped_host_are_ignored(make_app):
    app = make_app()
    hp, host = start_host(app)
    old_ui = host.ui
    hp.stop()
    run_thread(old_ui.ready, "0.tcp.eu.ngrok.io:1").join()
    run_thread(old_ui.connected, "Hayalet", "1.1.1.1").join()
    pump(app.root, lambda: False, 0.2)
    assert not hp.banner.winfo_ismapped() and hp.conn.get() == gui.ID_PLACEHOLDER and not hp.ready


def test_stop_calls_host_stop_and_kick_calls_host_kick(make_app):
    app = make_app()
    hp, host = start_host(app)
    assert hp.kick.instate(["disabled"])
    run_thread(host.ui.connected, "Ali", "1.2.3.4").join()
    assert pump(app.root, lambda: hp.banner.winfo_ismapped())
    hp.kick.invoke()
    assert host.calls == ["start", "kick"] and hp.kick.instate(["disabled"])
    hp.btn.invoke()
    assert host.calls == ["start", "kick", "stop"] and hp.host is None
    assert hp.btn.cget("text") == "Hazır Ol / Başlat" and not hp.banner.winfo_ismapped()
    assert hp.token_entry.instate(["!disabled"])


def test_host_constructor_failure_is_reported_not_raised(make_app):
    def boom(*a, **k):
        raise RuntimeError("Şifre ğüşiöçı")
    app = make_app(host_cls=boom)
    hp = app.host_panel
    hp.token.set(TOKEN)
    hp._toggle()
    assert hp.host is None and hp.state.get().startswith("Başlatılamadı: ") and not hp.btn.instate(["disabled"])


def test_reset_trust_clears_the_shared_list_in_place_and_saves(make_app):
    app = make_app()
    app.cfg["trusted"].extend(["a" * 64, "b" * 64])
    config.save(app.cfg)
    hp, host = start_host(app)
    listed = host.cfg["trusted"]
    hp._reset_trust()
    assert listed == [] and host.cfg["trusted"] is listed  # Host aynı listeyi görür
    assert config.load()["trusted"] == []
    assert "sıfırlandı" in hp.log.get()


def test_queue_pump_survives_exceptions_and_keeps_rearming(make_app, appdir):
    app = make_app()
    ran = []
    app.post(lambda: 1 / 0)
    app.post(lambda: ran.append(1))
    assert pump(app.root, lambda: ran)
    app.post(lambda: ran.append(2))  # after yeniden kurulmuş olmalı
    assert pump(app.root, lambda: len(ran) == 2)
    assert "ZeroDivisionError" in (appdir / "log.txt").read_text(encoding="utf-8")


# --- onay penceresi ---

def ask_consent(host, name="Ayşe'nin Dizüstü", addr="88.1.2.3"):
    """Motor iş parçacığı gibi ui.consent çağırır; (iş parçacığı, sonuç kutusu) döndürür."""
    box = []
    return run_thread(lambda: box.append(host.ui.consent(name, addr))), box


def consent_of(app, host):
    assert pump(app.root, lambda: host.ui.pending and host.ui.pending[0].dlg is not None)
    return host.ui.pending[0]


def test_consent_dialog_is_viewable_while_root_is_minimized(make_app):
    app = make_app()
    hp, host = start_host(app)
    app.root.iconify()
    app.root.update()
    assert app.root.state() == "iconic"
    t, box = ask_consent(host)
    c = consent_of(app, host)
    dlg = c.dlg
    assert dlg.winfo_viewable() and dlg.state() == "normal"
    assert dlg.transient() == ""  # transient olsaydı ana pencere simge durumundayken görünmezdi
    assert dlg.attributes("-topmost")
    assert app.root.state() == "normal"  # ana pencere de geri getirildi
    assert any("Ayşe'nin Dizüstü (88.1.2.3) bağlanmak istiyor" == s for s in texts(dlg))
    assert layout_problems(dlg) == []
    c.allow_btn.invoke()
    t.join(3)
    assert box == [True] and not dlg.winfo_exists()


@pytest.mark.parametrize("how,expected", [("allow", True), ("deny", False), ("close", False), ("escape", False)])
def test_consent_decisions(make_app, how, expected):
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    c = consent_of(app, host)
    if how == "allow":
        c.allow_btn.invoke()
    elif how == "deny":
        c.deny_btn.invoke()
    elif how == "close":
        c.dlg.tk.eval(c.dlg.protocol("WM_DELETE_WINDOW"))  # pencere kapatma düğmesi
    else:
        c.dlg.focus_force()
        c.dlg.update()
        c.dlg.event_generate("<Escape>")
    t.join(3)
    assert not t.is_alive() and box == [expected]
    assert not c.dlg.winfo_exists() and host.ui.pending == []


def test_consent_default_focus_is_the_safe_button(make_app):
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    c = consent_of(app, host)
    # işletim sistemi odak vermese de Tk'nin bu pencere için odak hedefi: yanlışlıkla Boşluk reddeder, izin vermez
    assert str(c.dlg.tk.call("focus", "-lastfor", c.dlg)) == str(c.deny_btn)
    c.deny_btn.invoke()
    t.join(3)


def test_consent_times_out_to_deny(make_app, monkeypatch):
    monkeypatch.setattr(gui, "CONSENT_SECONDS", 1)
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    c = consent_of(app, host)
    assert any("1 sn" in s for s in texts(c.dlg))
    assert pump(app.root, lambda: box, timeout=4)
    assert box == [False] and not c.dlg.winfo_exists()


def test_stopping_the_host_denies_a_pending_consent_immediately(make_app):
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    c = consent_of(app, host)
    start = time.monotonic()
    hp.stop()
    t.join(3)
    assert box == [False] and time.monotonic() - start < 1
    assert not c.dlg.winfo_exists()


def test_consent_after_stop_is_denied_without_a_dialog(make_app):
    app = make_app()
    hp, host = start_host(app)
    ui = host.ui
    hp.stop()
    box = []
    run_thread(lambda: box.append(ui.consent("Ali", "1.2.3.4"))).join(2)
    assert box == [False] and ui.pending == []


def test_consent_posted_but_denied_before_it_is_shown_never_appears(make_app):
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    assert pump(app.root, lambda: host.ui.pending, 2)  # istek kuyruğa konmuş, henüz boşaltılmamış olabilir
    host.ui.cancel_consent()
    t.join(3)
    assert box == [False]
    pump(app.root, lambda: False, 0.1)
    assert host.ui.pending == [] and not [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]


# --- Client paneli ---

def test_client_panel_requires_id_and_password(make_app):
    app = make_app()
    cp = app.client_panel
    cp._toggle()
    assert "gerekli" in cp.state.get() and not StubClient.made
    cp.conn.set("host:1")
    cp._toggle()
    assert "gerekli" in cp.state.get() and not StubClient.made
    assert str(cp.state_lbl.cget("style")) == "Bad.TLabel"


def test_client_relay_address_must_be_host_port(make_app):
    app = make_app()
    cp = app.client_panel
    cp.conn.set("123 456 789")
    cp.pw.set("ABCD2345EFGH")
    cp.relay.set("vps-yok-port")
    cp._toggle()
    assert "host:port" in cp.state.get() and not StubClient.made


def test_client_connect_passes_arguments_saves_relay_and_shows_viewer_on_first_frame(make_app):
    app = make_app()
    cp = app.client_panel
    app.show(cp)
    cp.conn.set("  123 456 789 ")
    cp.pw.set(" ABCD2345EFGH ")
    cp.relay.set("vps.example.com:9000")
    cp.relay_token.set("gizli")
    cp._toggle()
    client = StubClient.made[-1]
    assert client.args == ("123 456 789", "ABCD2345EFGH", app.cfg["uid"], ("vps.example.com", 9000, "gizli"))
    assert client.calls == ["start"] and cp.btn.cget("text") == "Vazgeç"
    assert cp.id_entry.instate(["disabled"]) and cp.back.instate(["disabled"])
    saved = config.load()
    assert saved["relay"] == "vps.example.com:9000" and saved["relay_token"] == "gizli"
    assert not cp.viewer.top.winfo_viewable()  # ilk kareye kadar görünmez
    run_thread(client.ui.status, "Host onayı bekleniyor…").join()
    assert pump(app.root, lambda: cp.state.get() == "Host onayı bekleniyor…")
    assert cp.viewer.status_lbl.cget("text") == "Host onayı bekleniyor…"
    run_thread(client.ui.frame, make_frame()).join()
    assert pump(app.root, lambda: cp.viewer.shown)
    assert cp.viewer.top.winfo_viewable() and cp.state.get() == "Bağlandı."


def test_client_without_relay_passes_none(make_app):
    app = make_app()
    cp = app.client_panel
    cp.conn.set("0.tcp.eu.ngrok.io:12345")
    cp.pw.set("ABCD2345EFGH")
    cp._toggle()
    assert StubClient.made[-1].args[3] is None


def test_fatal_close_closes_viewer_and_shows_error_retryable_keeps_it(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    run_thread(client.ui.closed, "lost", "Bağlantı koptu, yeniden deneniyor…", False).join()
    assert pump(app.root, lambda: v.status_lbl.cget("text").startswith("Bağlantı koptu"))
    assert cp.viewer is v and v.top.winfo_exists() and cp.btn.cget("text") == "Vazgeç"
    run_thread(client.ui.frame, make_frame(color=(10, 10, 10))).join()  # yeniden bağlanınca kareler sürer
    pump(app.root, lambda: False, 0.1)
    run_thread(client.ui.closed, "auth", "Şifre yanlış.", True).join()
    assert pump(app.root, lambda: cp.viewer is None)
    assert not v.top.winfo_exists() and client.calls == ["start", "stop"]
    assert cp.state.get() == "Şifre yanlış." and str(cp.state_lbl.cget("style")) == "Bad.TLabel"
    assert cp.btn.cget("text") == "Bağlan" and cp.id_entry.instate(["!disabled"]) and not cp.back.instate(["disabled"])


def test_closed_before_any_frame_returns_to_the_panel(make_app):
    app = make_app()
    cp = app.client_panel
    cp.conn.set("0.tcp.eu.ngrok.io:12345")
    cp.pw.set("ABCD2345EFGH")
    cp._toggle()
    client = StubClient.made[-1]
    run_thread(client.ui.closed, "unreachable", "Host'a ulaşılamadı.", True).join()
    assert pump(app.root, lambda: cp.client is None)
    assert cp.state.get() == "Host'a ulaşılamadı." and not [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]


def test_stopped_code_is_ignored(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    run_thread(client.ui.closed, "stopped", "Durduruldu.", True).join()
    pump(app.root, lambda: False, 0.15)
    assert cp.viewer is v


def test_vazgec_stops_the_client(make_app):
    app = make_app()
    cp = app.client_panel
    cp.conn.set("host:1")
    cp.pw.set("x")
    cp._toggle()
    client = StubClient.made[-1]
    cp.btn.invoke()
    assert client.calls == ["start", "stop"] and cp.client is None and cp.state.get() == "Vazgeçildi."
    assert cp.cui is None


def test_late_frames_after_stop_are_dropped(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    ui = client.ui
    cp.stop()
    run_thread(ui.frame, make_frame()).join()  # patlamamalı
    run_thread(ui.status, "geç").join()
    pump(app.root, lambda: False, 0.1)


def test_client_constructor_failure_is_reported(make_app):
    def boom(*a, **k):
        raise ValueError("kötü")
    app = make_app(client_cls=boom)
    cp = app.client_panel
    cp.conn.set("host:1")
    cp.pw.set("x")
    cp._toggle()
    assert cp.client is None and cp.state.get().startswith("Bağlanılamadı: ")


# --- görüntüleyici: sığdırma ve fare eşlemesi ---

@pytest.mark.parametrize("frame,canvas,expected", [
    ((1280, 720), (1280, 720), (1280, 720, 0, 0)),
    ((1280, 720), (800, 800), (800, 450, 0, 175)),  # üst/alt siyah şerit
    ((1280, 720), (1600, 600), (1067, 600, 266, 0)),  # yan şeritler
    ((1280, 720), (640, 360), (640, 360, 0, 0)),  # yalnız küçültme
    ((1280, 720), (3840, 2160), (1920, 1080, 960, 540)),  # büyütme en çok 1.5x
    ((1280, 720), (1920, 1080), (1920, 1080, 0, 0)),
    ((640, 480), (1000, 1000), (960, 720, 20, 140)),
    ((1280, 720), (10, 10), (10, 6, 0, 2)),
])
def test_fit_rect(frame, canvas, expected):
    assert gui.fit_rect(*frame, *canvas) == expected


def test_viewer_letterbox_mapping_of_mouse_events(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    x, y, dw, dh = v._rect
    exp = gui.fit_rect(1280, 720, cw, ch)
    assert (dw, dh, x, y) == exp and dw <= cw and dh <= ch and (dw == cw or dh == ch)
    assert abs(dw / dh - 1280 / 720) < 0.01 and x == (cw - dw) // 2 and y == (ch - dh) // 2
    assert y > 20  # bu boyutta kare üstten/alttan şeritlidir
    c = v.canvas
    client.sent.clear()

    def click(px, py, b=1):
        c.event_generate(f"<ButtonPress-{b}>", x=px, y=py)
        c.event_generate(f"<ButtonRelease-{b}>", x=px, y=py)
        return [e for e in client.sent if e["t"] == "b"]

    got = click(x + dw // 2, y + dh // 2)  # tam orta
    assert [(e["b"], e["d"]) for e in got] == [("l", 1), ("l", 0)]
    assert abs(got[0]["x"] - 0.5) < 0.01 and abs(got[0]["y"] - 0.5) < 0.01
    client.sent.clear()
    got = click(x, y)  # sol üst piksel
    assert got[0]["x"] < 0.01 and got[0]["y"] < 0.01
    client.sent.clear()
    got = click(x + dw - 1, y + dh - 1)  # sağ alt piksel
    assert got[0]["x"] > 0.99 and got[0]["y"] > 0.99 and got[0]["x"] <= 1.0 and got[0]["y"] <= 1.0
    client.sent.clear()
    assert click(cw // 2, 3) == []  # üstteki siyah şerit: ne basış ne bırakış iletilir
    assert click(cw // 2, ch - 3, b=3) == []
    assert client.sent == []
    # sağ ve orta düğme eşlemesi
    client.sent.clear()
    assert [e["b"] for e in click(x + 10, y + 10, 3)] == ["r", "r"]
    client.sent.clear()
    assert [e["b"] for e in click(x + 10, y + 10, 2)] == ["m", "m"]


def test_viewer_motion_is_clamped_and_press_outside_does_not_release_inside(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    x, y, dw, dh = v._rect
    c = v.canvas
    client.sent.clear()
    c.event_generate("<Motion>", x=x + dw // 4, y=y + dh // 2)
    c.event_generate("<Motion>", x=cw // 2, y=0)  # siyah şerit: kenara sıkıştırılır
    c.event_generate("<Motion>", x=cw + 50, y=ch + 50)  # tuval dışı
    m = [e for e in client.sent if e["t"] == "m"]
    assert len(m) == 3 and abs(m[0]["x"] - 0.25) < 0.01 and abs(m[0]["y"] - 0.5) < 0.01
    assert m[1]["y"] == 0.0 and 0.0 < m[1]["x"] < 1.0
    assert m[2]["x"] == 1.0 and m[2]["y"] == 1.0
    client.sent.clear()
    c.event_generate("<ButtonPress-1>", x=cw // 2, y=2)  # şeritte bas
    c.event_generate("<ButtonRelease-1>", x=x + 20, y=y + 20)  # görüntü içinde bırak
    assert [e for e in client.sent if e["t"] == "b"] == []
    c.event_generate("<ButtonPress-1>", x=x + 5, y=y + 5)  # içeride bas, dışarıda bırak: bırakış sıkıştırılıp iletilir
    c.event_generate("<ButtonRelease-1>", x=cw + 40, y=y + 5)
    b = [e for e in client.sent if e["t"] == "b"]
    assert [e["d"] for e in b] == [1, 0] and b[1]["x"] == 1.0


def test_viewer_ignores_the_mouse_before_the_first_frame(make_app):
    app = make_app()
    cp = app.client_panel
    cp.conn.set("host:1")
    cp.pw.set("x")
    cp._toggle()
    v, client = cp.viewer, StubClient.made[-1]
    v.canvas.event_generate("<Motion>", x=50, y=50)
    v.canvas.event_generate("<ButtonPress-1>", x=50, y=50)
    assert client.sent == []


def test_viewer_wheel_passes_delta_as_is(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    settle(app, v, "820x700+20+20")
    client.sent.clear()
    for delta in (-120, 120, 8, -37, 3600, 0):
        v.canvas.event_generate("<MouseWheel>", delta=delta, x=100, y=100)
    assert [e["d"] for e in client.sent if e["t"] == "w"] == [-120, 120, 8, -37, 1200]  # 0 yollanmaz, büyük değer sıkıştırılır


def test_modifier_click_goes_through_before_mouse(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    x, y, dw, dh = v._rect
    c = v.canvas
    c.focus_force()
    c.update()
    client.sent.clear()
    c.event_generate("<KeyPress>", keysym="Control_L")
    assert client.sent == []  # tembel
    c.event_generate("<ButtonPress-1>", x=x + 30, y=y + 30)
    c.event_generate("<ButtonRelease-1>", x=x + 30, y=y + 30)
    c.event_generate("<KeyRelease>", keysym="Control_L")
    kinds = [(e["t"], e.get("vk") or e.get("b"), e.get("d")) for e in client.sent]
    assert kinds == [("k", 0x11, 1), ("b", "l", 1), ("b", "l", 0), ("k", 0x11, 0)]


# --- görüntüleyici: kareler, iş parçacığı, tek PhotoImage ---

def test_frames_are_resized_off_the_tk_thread_and_one_photoimage_is_reused(make_app, monkeypatch):
    main_id = threading.get_ident()
    resized_on = []
    orig_resize = Image.Image.resize

    def spy_resize(self, size, *a, **k):
        resized_on.append(threading.get_ident())
        return orig_resize(self, size, *a, **k)

    photos = []

    class CountingPhoto(ImageTk.PhotoImage):
        def __init__(self, *a, **k):
            photos.append(self)
            self.pastes = 0
            super().__init__(*a, **k)

        def paste(self, im):
            self.pastes += 1
            super().paste(im)

    monkeypatch.setattr(Image.Image, "resize", spy_resize)
    monkeypatch.setattr(gui.ImageTk, "PhotoImage", CountingPhoto)
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    assert len(photos) >= 1
    base = len(photos)
    first = v._photo
    resized_on.clear()
    colors = [(200, 40, 40), (40, 200, 40), (40, 40, 200)]
    for color in colors:  # aynı boyutta ardışık kareler (motor iş parçacığından)
        run_thread(client.ui.frame, make_frame(color=color)).join()
        assert pump(app.root, lambda: pixel(v, v._rect[2] // 2, v._rect[3] - 5) == color)
    assert resized_on and main_id not in resized_on  # ölçekleme yalnızca motor iş parçacığında
    assert len(photos) == base and v._photo is first and first.pastes >= len(colors)  # tek PhotoImage, yalnızca paste
    # tuval boyutu değişince (ve yalnızca o zaman) yeni PhotoImage
    settle(app, v, "700x600+20+20")
    assert len(photos) == base + 1 and v._photo is not first


def test_scaled_frame_is_kept_in_aspect_and_pixels_land_in_the_right_place(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    x, y, dw, dh = v._rect
    assert v._photo.width() == dw and v._photo.height() == dh
    assert pixel(v, dw // 2, 2) == (240, 200, 40)  # üst şerit
    assert pixel(v, dw // 2, dh - 3) == (30, 90, 160)
    assert v.canvas.coords(v._item) == [float(x), float(y)]


def test_last_frame_is_refit_when_the_window_is_resized(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    cw, ch = settle(app, v, "820x700+20+20")
    r1 = v._rect
    cw2, ch2 = settle(app, v, "1000x500+20+20")  # kare yok, yalnızca pencere büyüdü/küçüldü
    r2 = v._rect
    assert r1 != r2 and (r2[2], r2[3], r2[0], r2[1]) == gui.fit_rect(1280, 720, cw2, ch2)


def test_upscale_is_capped_at_one_and_a_half(make_app):
    app = make_app()
    cp, client, v = open_viewer(app, size=(320, 200))
    cw, ch = settle(app, v, "900x700+20+20")
    x, y, dw, dh = v._rect
    assert (dw, dh) == (480, 300)  # 320x200 * 1.5
    assert x == (cw - 480) // 2


def test_newest_frame_wins_when_several_arrive_between_ticks(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    settle(app, v, "820x700+20+20")
    for color in ((255, 0, 0), (0, 255, 0), (1, 2, 250)):
        run_thread(client.ui.frame, make_frame(color=color)).join()
    assert pump(app.root, lambda: pixel(v, v._rect[2] // 2, v._rect[3] - 5) == (1, 2, 250))


def test_frame_callback_swallows_decode_errors(make_app, appdir):
    app = make_app()
    cp, client, v = open_viewer(app)

    class Broken:
        width = height = 100
        size = (100, 100)

        def resize(self, *a, **k):
            raise OSError("bozuk kare")
    run_thread(client.ui.frame, Broken()).join()  # motor iş parçacığını düşürmemeli
    assert "bozuk kare" in (appdir / "log.txt").read_text(encoding="utf-8")


# --- görüntüleyici: klavye ---

def key(keysym, keycode, char=""):
    return SimpleNamespace(keysym=keysym, keycode=keycode, char=char)


def test_viewer_key_handlers_return_break_and_feed_the_keymapper(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    client.sent.clear()
    for ev in (key("scedilla", 186, "ş"), key("Iabovedot", 222, "İ"), key("Tab", 9, "\t"), key("Return", 13, "\r")):
        assert v._on_key_press(ev) == "break" and v._on_key_release(ev) == "break"
    assert client.sent == [{"t": "c", "c": "ş"}, {"t": "c", "c": "İ"},
                           {"t": "k", "vk": 9, "d": 1}, {"t": "k", "vk": 9, "d": 0},
                           {"t": "k", "vk": 13, "d": 1}, {"t": "k", "vk": 13, "d": 0}]
    for seq in ("<KeyPress>", "<KeyRelease>"):
        assert v.canvas.bind(seq)  # tuvale bağlı; "break" Tab gezintisi gibi varsayılanları keser


def test_viewer_gets_keyboard_focus_and_forwards_real_tk_key_events(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    c = v.canvas
    assert app.root.focus_get() is c  # ilk kare gelince odak tuvalde
    client.sent.clear()
    c.event_generate("<KeyPress>", keysym="a")
    c.event_generate("<KeyRelease>", keysym="a")
    c.event_generate("<KeyPress>", keysym="Shift_L")
    c.event_generate("<KeyPress>", keysym="A")
    c.event_generate("<KeyRelease>", keysym="A")
    c.event_generate("<KeyRelease>", keysym="Shift_L")
    c.event_generate("<KeyPress>", keysym="Left")
    c.event_generate("<KeyRelease>", keysym="Left")
    c.event_generate("<KeyPress>", keysym="F5")
    c.event_generate("<KeyRelease>", keysym="F5")
    assert client.sent == [{"t": "c", "c": "a"}, {"t": "c", "c": "A"},
                           {"t": "k", "vk": 37, "d": 1}, {"t": "k", "vk": 37, "d": 0},
                           {"t": "k", "vk": 116, "d": 1}, {"t": "k", "vk": 116, "d": 0}]


def test_clicking_the_canvas_refocuses_it(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    settle(app, v, "820x700+20+20")
    entry = ttk.Entry(v.top)
    entry.pack()
    entry.focus_force()
    app.root.update()
    assert app.root.focus_get() is entry
    v.canvas.event_generate("<ButtonPress-1>", x=5, y=5)  # siyah şerit de olsa odak alınır
    assert app.root.focus_get() is v.canvas


def test_focus_out_releases_forwarded_keys(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    c = v.canvas
    c.event_generate("<KeyPress>", keysym="Control_L")
    c.event_generate("<KeyPress>", keysym="Left")  # Ctrl + Left iletildi
    client.sent.clear()
    c.event_generate("<FocusOut>")
    assert client.sent == [{"t": "k", "vk": 37, "d": 0}, {"t": "k", "vk": 0x11, "d": 0}]
    assert v.keys.sent_mods == [] and v.keys.keys_down == [] and v.keys.mods_held == {}


def test_closing_the_viewer_releases_keys_and_stops_the_client_without_blocking(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    v.canvas.event_generate("<KeyPress>", keysym="Right")
    client.sent.clear()
    start = time.monotonic()
    v.close()  # pencere kapatma / Bağlantıyı Kes
    assert time.monotonic() - start < 0.5
    assert client.sent == [{"t": "k", "vk": 39, "d": 0}]  # Host'ta basılı tuş kalmaz
    assert client.calls == ["start", "stop"] and cp.client is None and not v.top.winfo_exists()
    assert cp.state.get() == "Bağlantı kapatıldı." and cp.btn.cget("text") == "Bağlan"
    v.close()  # ikinci çağrı zararsız
    assert client.calls == ["start", "stop"]


def test_viewer_disconnect_button_closes_it(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    buttons = [w for w in descendants(v.top) if w.winfo_class() == "TButton"]
    assert [b.cget("text") for b in buttons] == ["Bağlantıyı Kes"]
    buttons[0].invoke()
    assert client.calls == ["start", "stop"] and cp.viewer is None


def test_locked_screen_message_is_shown_in_the_viewer_strip(make_app):
    app = make_app()
    cp, client, v = open_viewer(app)
    run_thread(client.ui.status, "Host ekranı kilitli / UAC açık").join()
    assert pump(app.root, lambda: v.status_lbl.cget("text") == "Host ekranı kilitli / UAC açık")
    assert any("kilitli" in t for t in texts(v.top))


# --- kapanış: motorlar bloklamadan durur ---

def test_closing_the_app_stops_engines_and_waits_for_teardown_threads(make_app):
    done = threading.Event()

    class SlowHost(StubHost):
        def stop(self):
            super().stop()  # stop() hemen döner; kapatma kendi iş parçacığında sürer

            def teardown():
                time.sleep(0.4)
                done.set()
            threading.Thread(target=teardown, name="teardown", daemon=True).start()

    app = make_app(host_cls=SlowHost)
    hp, host = start_host(app)
    cp, client, v = open_viewer(app)
    start = time.monotonic()
    app.close()
    took = time.monotonic() - start
    assert done.is_set() and 0.3 < took < 3  # kapatma iş parçacığı bitmeden pencere yıkılmadı
    assert host.calls == ["start", "stop"] and client.calls == ["start", "stop"]
    with pytest.raises(tk.TclError):
        app.root.winfo_exists()


def test_closing_gives_up_waiting_after_close_wait(make_app, monkeypatch):
    monkeypatch.setattr(gui, "CLOSE_WAIT", 0.5)
    release = threading.Event()

    class HungHost(StubHost):
        def stop(self):
            super().stop()
            threading.Thread(target=release.wait, args=(30,), name="hung-teardown", daemon=True).start()

    app = make_app(host_cls=HungHost)
    hp, host = start_host(app)
    t0 = time.monotonic()
    app.close()
    try:
        assert 0.4 < time.monotonic() - t0 < 2.5  # sonsuza dek beklemez, os._exit de yok
        with pytest.raises(tk.TclError):
            app.root.winfo_exists()
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == "hung-teardown":
                t.join(2)


def test_panel_stop_returns_immediately_even_if_engine_teardown_is_slow(make_app):
    release = threading.Event()

    class HungHost(StubHost):
        def stop(self):
            super().stop()
            threading.Thread(target=release.wait, args=(30,), name="hung-teardown", daemon=True).start()

    app = make_app(host_cls=HungHost)
    hp, host = start_host(app)
    start = time.monotonic()
    hp.stop()
    try:
        assert time.monotonic() - start < 0.3
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == "hung-teardown":
                t.join(2)


def test_closing_denies_a_pending_consent_and_unblocks_the_worker(make_app):
    app = make_app()
    hp, host = start_host(app)
    t, box = ask_consent(host)
    consent_of(app, host)
    start = time.monotonic()
    app.close()
    t.join(3)
    assert box == [False] and time.monotonic() - start < 3 and not t.is_alive()


def test_close_is_idempotent_and_survives_engine_stop_errors(make_app):
    class BadHost(StubHost):
        def stop(self):
            raise RuntimeError("stop patladı")

    app = make_app(host_cls=BadHost)
    start_host(app)
    app.close()
    app.close()  # ikinci çağrı sessizce döner


# --- günlük ve std akımları ---

def test_hooks_log_tk_thread_and_main_exceptions_in_utf8(make_app, appdir, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    app = make_app()
    gui._install_hooks(app.root)

    def tk_cb():
        raise RuntimeError("Tk geri çağrısı: ŞİFRE ğüşiöçı")
    app.root.after(0, tk_cb)
    app.root.update()
    t = threading.Thread(target=lambda: 1 / 0)
    t.start()
    t.join()
    try:
        raise KeyError("ana iş parçacığı İ")
    except KeyError:
        sys.excepthook(*sys.exc_info())
    text = (appdir / "log.txt").read_text(encoding="utf-8")
    assert "Tk geri çağrısı: ŞİFRE ğüşiöçı" in text and "ZeroDivisionError" in text and "ana iş parçacığı İ" in text


def test_log_failure_never_raises(monkeypatch):
    def deny(*a, **k):
        raise PermissionError("yazılamaz")
    monkeypatch.setattr(os, "makedirs", deny)
    gui._log("sessiz")  # APPDIR yazılamasa da uygulama düşmez


def test_fix_streams_replaces_none_with_devnull(monkeypatch):
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, None)
    gui._fix_streams()
    opened = [sys.stdin, sys.stdout, sys.stderr]
    try:
        assert all(f is not None for f in opened)
        sys.stdout.write("pyngrok ilerleme çubuğu")
        sys.stdout.flush()
        sys.stderr.write("x")
        sys.stderr.flush()
        assert sys.stdin.read() == ""
    finally:
        for f in opened:
            f.close()


def test_fix_streams_leaves_real_streams_alone(monkeypatch):
    before = (sys.stdin, sys.stdout, sys.stderr)
    gui._fix_streams()
    assert (sys.stdin, sys.stdout, sys.stderr) == before


# --- main.py --selftest ---

def test_selftest_cli_writes_ok_and_exits_zero(tmp_path):
    pytest.importorskip("uzakmasa.host")
    pytest.importorskip("uzakmasa.client")
    out = tmp_path / "selftest.txt"
    p = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "--selftest", str(out)], cwd=ROOT,
                       capture_output=True, timeout=120, env={**os.environ, "LOCALAPPDATA": str(tmp_path)})
    report = out.read_text(encoding="utf-8")
    assert p.returncode == 0, report + p.stderr.decode("utf-8", "replace")
    assert report.splitlines()[-1] == "OK" and "FAIL" not in report
    for name in ("içe aktarma", "scrypt", "el sıkışma", "Tk + ImageTk", "pyngrok", "INPUT boyutu"):
        assert f"ok {name}" in report


def test_selftest_reports_failures_and_exit_code_one(tmp_path, monkeypatch):
    sys.path.insert(0, ROOT)
    try:
        import main as entry
    finally:
        sys.path.remove(ROOT)

    def boom():
        raise AssertionError("kasıtlı")
    monkeypatch.setattr(entry, "CHECKS", (("iyi", lambda: "tamam"), ("kötü", boom), ("atla", lambda: (_ for _ in ()).throw(entry._Skip("ekran yok")))))
    out = tmp_path / "r.txt"
    assert entry.selftest(str(out)) == 1
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines == ["ok iyi: tamam", "FAIL kötü: AssertionError: kasıtlı", "skip atla: ekran yok", "FAILED: 1"]


# --- ekran görüntüleri (yalnızca UZAK_SHOTS ayarlıysa; yalnızca KENDİ pencerelerimiz) ---

def grab(w, name):
    """Pencerenin YALNIZ istemci alanını kaydeder (görünmez kenarlık/gölge dışarıdaki pencerelerden piksel taşırdı);
    pencere en üstte tutulur."""
    w.attributes("-topmost", True)
    w.lift()
    w.update()
    time.sleep(0.5)
    w.update()
    box = (w.winfo_rootx(), w.winfo_rooty(), w.winfo_rootx() + w.winfo_width(), w.winfo_rooty() + w.winfo_height())
    img = ImageGrab.grab(bbox=box)
    img.save(os.path.join(SHOTS, name + ".png"))
    assert img.size[0] > 100 and img.size[1] > 100
    return img


def fake_desktop(w=1280, h=720):
    """Kendi çizdiğimiz sahte uzak masaüstü (kullanıcının gerçek ekranı değil)."""
    img = Image.new("RGB", (w, h), (32, 74, 135))
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("segoeui.ttf", 28)
    except OSError:
        f = ImageFont.load_default()
    d.rectangle((0, h - 48, w, h), fill=(20, 20, 24))
    d.rectangle((120, 90, 760, 470), fill=(245, 245, 245), outline=(200, 200, 200))
    d.rectangle((120, 90, 760, 130), fill=(60, 120, 200))
    d.text((136, 94), "Uzak ekran: ş ğ ı İ ö ü ç @ €", fill="white", font=f)
    d.text((140, 170), "Merhaba dünya!", fill=(20, 20, 20), font=f)
    d.rectangle((140, 240, 400, 290), outline=(60, 120, 200), width=3)
    return img


@pytest.mark.skipif(not SHOTS, reason="UZAK_SHOTS=<klasör> ile açılır")
def test_screenshots(make_app, monkeypatch):
    os.makedirs(SHOTS, exist_ok=True)
    app = make_app()
    root = app.root
    root.geometry("+60+40")
    root.update()
    grab(root, "1-launcher")
    app.show(app.host_panel)
    root.update()
    grab(root, "2-host-idle")
    app.host_panel.mode.set("relay")
    app.host_panel.relay.set("203.0.113.7:9000")
    root.update()
    grab(root, "2b-host-relay-mode")
    app.host_panel.mode.set("ngrok")
    hp, host = start_host(app)
    ui = host.ui
    run_thread(lambda: (ui.log("ngrok hazırlanıyor (ilk çalıştırmada indirilir)…"), ui.ready("0.tcp.eu.ngrok.io:12345"),
                        ui.stats(123 * 1024 * 1024), ui.log("Bağlantı bekleniyor"))).join()
    assert pump(root, lambda: hp.ready)
    grab(root, "3-host-ready")
    run_thread(ui.connected, "Ayşe'nin Dizüstü (Şişli)", "88.12.4.5").join()
    run_thread(ui.stats, 850 * 1024 * 1024).join()
    assert pump(root, lambda: hp.banner.winfo_ismapped() and "850" in hp.info.get())
    grab(root, "4-host-connected")
    t, box = ask_consent(host)
    c = consent_of(app, host)
    c.dlg.geometry("+300+260")
    grab(c.dlg, "5-consent")
    c.deny_btn.invoke()
    t.join(3)
    hp.stop()
    app.show(app.client_panel)
    cp = app.client_panel
    cp.conn.set("0.tcp.eu.ngrok.io:12345")
    cp.pw.set("ABCD2345EFGH")
    root.update()
    grab(root, "6-client")
    cp._toggle()
    client = StubClient.made[-1]
    run_thread(client.ui.status, "Host onayı bekleniyor…").join()
    pump(root, lambda: cp.state.get().startswith("Host"))
    grab(root, "6b-client-connecting")
    frame = fake_desktop()
    run_thread(client.ui.frame, frame).join()
    assert pump(root, lambda: cp.viewer.shown)
    cp.viewer.top.geometry("1000x640+40+30")
    pump(root, lambda: False, 0.4)
    grab(cp.viewer.top, "7-viewer")
    run_thread(client.ui.status, "Host ekranı kilitli / UAC açık").join()
    pump(root, lambda: "kilitli" in cp.viewer.status_lbl.cget("text"))
    cp.viewer.top.geometry("700x700+40+30")  # kare şeritli
    pump(root, lambda: False, 0.4)
    grab(cp.viewer.top, "7b-viewer-letterbox-locked")
    assert layout_problems(cp.viewer.top) == []
