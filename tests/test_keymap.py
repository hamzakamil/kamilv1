"""keymap.py testleri: sentetik (keysym, keycode, char) üçlüleriyle Host'a giden olay akışı birebir doğrulanır.

Kullanıcının başlıca şartı "klavyeden yazabileyim": Türkçe metin Host'un düzeninden bağımsız 'c' olayı olmalı,
kısayollar ve yönlendirme tuşları 'k' olayı olmalı, hiçbir tuş Host'ta basılı kalmamalı.
"""
import random

import pytest

from uzakmasa.keymap import KeyMapper

SHIFT, CTRL, ALT, WIN = 0x10, 0x11, 0x12, 0x5B


def c(ch):
    return {"t": "c", "c": ch}


def kd(vk):
    return {"t": "k", "vk": vk, "d": 1}


def ku(vk):
    return {"t": "k", "vk": vk, "d": 0}


class Rig:
    """KeyMapper + kaydedilen olaylar; her adımda yalnızca o adımın ürettiği olayları döndürür."""

    def __init__(self):
        self.out = []
        self.m = KeyMapper(self.out.append)

    def _take(self):
        got, self.out[:] = list(self.out), []
        return got

    def down(self, keysym, keycode=0, char=""):
        self.m.key_press(keysym, keycode, char)
        return self._take()

    def up(self, keysym, keycode=0):
        self.m.key_release(keysym, keycode)
        return self._take()

    def mouse(self):
        self.m.before_mouse()
        return self._take()

    def focus_out(self):
        self.m.focus_out()
        return self._take()

    def clean(self):
        return not self.m.mods_held and not self.m.sent_mods and not self.m.keys_down


@pytest.fixture
def rig():
    return Rig()


# --- yazı: Türkçe karakterler yalnızca 'c' olayıdır ---

def test_turkish_letters_are_char_events_only(rig):
    keys = [("scedilla", 186, "ş"), ("gbreve", 219, "ğ"), ("udiaeresis", 221, "ü"), ("odiaeresis", 220, "ö"),
            ("ccedilla", 191, "ç"), ("idotless", 73, "ı")]
    got = []
    for keysym, keycode, ch in keys:
        got += rig.down(keysym, keycode, ch) + rig.up(keysym, keycode)
    # büyükler Shift ile: değiştirici basışı/bırakışı da hiçbir şey göndermez
    got += rig.down("Shift_L", 16)
    for keysym, keycode, ch in [("Iabovedot", 222, "İ"), ("Scedilla", 186, "Ş")]:
        got += rig.down(keysym, keycode, ch) + rig.up(keysym, keycode)
    got += rig.up("Shift_L", 16)
    assert got == [c(x) for x in "şğüöçıİŞ"]
    assert rig.clean()


def test_shift_a_gives_capital_a(rig):
    assert rig.down("Shift_L", 16) == []
    assert rig.down("A", 65, "A") == [c("A")]
    assert rig.up("A", 65) == []
    assert rig.up("Shift_L", 16) == []
    assert rig.clean()


def test_altgr_q_gives_at_sign_and_flushes_nothing(rig):
    assert rig.down("Control_L", 17) == []  # AltGr = Control_L + Alt_R
    assert rig.down("Alt_R", 18) == []
    assert rig.down("at", 81, "@") == [c("@")]
    assert rig.m.sent_mods == []  # Host'ta Ctrl+Alt basılmadı
    assert rig.up("at", 81) == []
    assert rig.up("Alt_R", 18) == []
    assert rig.up("Control_L", 17) == []
    assert rig.clean()


@pytest.mark.parametrize("keysym,keycode,ch", [
    ("EuroSign", 69, "€"), ("braceleft", 55, "{"), ("bracketleft", 56, "["), ("bracketright", 57, "]"),
    ("braceright", 48, "}"), ("backslash", 191, "\\"), ("bar", 189, "|"), ("onehalf", 53, "½"), ("lira", 84, "₺"),
])
def test_altgr_characters(rig, keysym, keycode, ch):
    rig.down("Control_L", 17)
    rig.down("Alt_R", 18)
    assert rig.down(keysym, keycode, ch) == [c(ch)]
    assert rig.up(keysym, keycode) == []
    assert rig.up("Control_L", 17) == [] and rig.up("Alt_R", 18) == []
    assert rig.clean()


@pytest.mark.parametrize("keysym,keycode,ch", [
    ("space", 32, " "), ("0", 48, "0"), ("7", 55, "7"), ("exclam", 49, "!"), ("quoteright", 50, "'"),
    ("asciicircum", 51, "^"), ("plus", 52, "+"), ("percent", 53, "%"), ("ampersand", 54, "&"), ("slash", 55, "/"),
    ("parenleft", 56, "("), ("equal", 48, "="), ("question", 191, "?"), ("underscore", 189, "_"), ("minus", 189, "-"),
    ("comma", 188, ","), ("period", 190, "."), ("KP_5", 101, "5"), ("KP_Add", 107, "+"), ("EuroSign", 69, "€"),
])
def test_printable_keys_are_char_events(rig, keysym, keycode, ch):
    assert rig.down(keysym, keycode, ch) == [c(ch)]
    assert rig.up(keysym, keycode) == []
    assert rig.clean()


def test_typing_the_acceptance_h_string_only_sends_chars(rig):
    text = "Merhaba şğüöçıİ ÇĞÜÖŞ @€ 123 !'^+%&/()=?_-"
    got = []
    for ch in text:
        got += rig.down("x", 65, ch) + rig.up("x", 65)
    assert got == [c(x) for x in text]


# --- yazıya dönüşmeyen tuşlar: basışta DOWN, bırakışta UP ---

@pytest.mark.parametrize("keysym,keycode,ch", [
    ("Return", 13, "\r"), ("KP_Enter", 13, "\r"), ("BackSpace", 8, "\x08"), ("Tab", 9, "\t"), ("Escape", 27, "\x1b"),
    ("Left", 37, ""), ("Up", 38, ""), ("Right", 39, ""), ("Down", 40, ""), ("Delete", 46, "\x7f"),
    ("Home", 36, ""), ("End", 35, ""), ("Prior", 33, ""), ("Next", 34, ""), ("Insert", 45, ""),
    ("F1", 112, ""), ("F5", 116, ""), ("F12", 123, ""), ("Caps_Lock", 20, ""), ("Num_Lock", 144, ""),
    ("Menu", 93, ""), ("Pause", 19, ""), ("Print", 44, ""),
])
def test_non_printable_keys_are_vk_down_then_up(rig, keysym, keycode, ch):
    assert rig.down(keysym, keycode, ch) == [kd(keycode)]
    assert rig.m.keys_down == [keycode]
    assert rig.up(keysym, keycode) == [ku(keycode)]
    assert rig.clean()


def test_ctrl_c(rig):
    assert rig.down("Control_L", 17) == []  # tembel: henüz gönderilmez
    assert rig.down("c", 67, "\x03") == [kd(CTRL), kd(67)]  # Ctrl DOWN önce boşaltılır
    assert rig.up("c", 67) == [ku(67)]
    assert rig.up("Control_L", 17) == [ku(CTRL)]
    assert rig.clean()


def test_alt_f(rig):
    assert rig.down("Alt_L", 18) == []
    assert rig.down("f", 70, "f") == [kd(ALT), kd(70)]  # tek başına Alt kısayoldur, yazı değil
    assert rig.up("f", 70) == [ku(70)]
    assert rig.up("Alt_L", 18) == [ku(ALT)]
    assert rig.clean()


def test_ctrl_space_and_shift_tab_are_shortcuts_not_text(rig):
    rig.down("Control_L", 17)
    assert rig.down("space", 32, " ") == [kd(CTRL), kd(32)]
    assert rig.up("space", 32) == [ku(32)]
    assert rig.up("Control_L", 17) == [ku(CTRL)]
    rig.down("Shift_L", 16)
    assert rig.down("ISO_Left_Tab", 9, "") == [kd(SHIFT), kd(9)]
    assert rig.up("ISO_Left_Tab", 9) == [ku(9)]
    assert rig.up("Shift_L", 16) == [ku(SHIFT)]
    assert rig.clean()


@pytest.mark.parametrize("first,second", [(("Control_L", 17), ("Shift_L", 16)), (("Shift_L", 16), ("Control_L", 17))])
def test_ctrl_shift_esc_follows_press_order(rig, first, second):
    vk = {"Control_L": CTRL, "Shift_L": SHIFT}
    assert rig.down(*first) == [] and rig.down(*second) == []
    assert rig.down("Escape", 27, "\x1b") == [kd(vk[first[0]]), kd(vk[second[0]]), kd(27)]
    assert rig.up("Escape", 27) == [ku(27)]
    assert rig.up(*second) == [ku(vk[second[0]])]
    assert rig.up(*first) == [ku(vk[first[0]])]
    assert rig.clean()


def test_modifiers_released_before_the_key(rig):
    rig.down("Control_L", 17)
    rig.down("Shift_L", 16)
    assert rig.down("Escape", 27, "\x1b") == [kd(CTRL), kd(SHIFT), kd(27)]
    assert rig.up("Control_L", 17) == [ku(CTRL)]
    assert rig.up("Shift_L", 16) == [ku(SHIFT)]
    assert rig.up("Escape", 27) == [ku(27)]
    assert rig.clean()


def test_altgr_with_a_non_printable_key_sends_ctrl_and_alt(rig):
    rig.down("Control_L", 17)
    rig.down("Alt_R", 18)
    assert rig.down("Left", 37, "") == [kd(CTRL), kd(ALT), kd(37)]
    assert rig.up("Left", 37) == [ku(37)]
    assert rig.up("Alt_R", 18) == [ku(ALT)]
    assert rig.up("Control_L", 17) == [ku(CTRL)]
    assert rig.clean()


def test_windows_key_is_a_modifier_that_never_blocks_text(rig):
    # Tk-Windows Win tuşuna Win_L der; Super_L başka Tk sürümleri için
    for keysym in ("Win_L", "Super_L"):
        assert rig.down(keysym, 91) == [] and rig.up(keysym, 91) == []
        rig.down(keysym, 91)
        assert rig.down("a", 65, "a") == [c("a")]
        assert rig.down("Left", 37, "") == [kd(WIN), kd(37)]
        assert rig.up("Left", 37) == [ku(37)]
        assert rig.up(keysym, 91) == [ku(WIN)]
        rig.up("a", 65)
    assert rig.clean()


# --- fare: Ctrl+tık, Shift+tık ---

@pytest.mark.parametrize("keysym,keycode,vk", [("Control_L", 17, CTRL), ("Shift_L", 16, SHIFT), ("Alt_L", 18, ALT)])
def test_modifier_click_flushes_once_and_releases_on_key_release(rig, keysym, keycode, vk):
    assert rig.down(keysym, keycode) == []
    assert rig.mouse() == [kd(vk)]  # tıklamadan önce bir kez
    assert rig.mouse() == []        # ikinci düğme/tekerlek olayı yeniden göndermez
    assert rig.up(keysym, keycode) == [ku(vk)]
    assert rig.mouse() == []
    assert rig.clean()


def test_mouse_without_modifiers_sends_nothing(rig):
    assert rig.mouse() == []
    rig.down("a", 65, "a")
    assert rig.mouse() == []


def test_click_then_key_does_not_resend_modifier(rig):
    rig.down("Control_L", 17)
    assert rig.mouse() == [kd(CTRL)]
    assert rig.down("c", 67, "\x03") == [kd(67)]  # zaten iletilmişti
    assert rig.up("c", 67) == [ku(67)]
    assert rig.up("Control_L", 17) == [ku(CTRL)]


# --- tek başına değiştirici, otomatik tekrar ---

@pytest.mark.parametrize("keysym", ["Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Win_L",
                                    "Win_R", "Super_L", "Super_R"])
def test_modifier_tapped_alone_sends_nothing(rig, keysym):
    assert rig.down(keysym, 16) == []
    assert rig.up(keysym, 16) == []
    assert rig.clean()


def test_held_character_key_repeats_characters(rig):
    got = rig.down("a", 65, "a") + rig.down("a", 65, "a") + rig.down("a", 65, "a") + rig.up("a", 65)
    assert got == [c("a")] * 3


def test_held_vk_key_repeats_down_events_and_one_up(rig):
    got = [rig.down("BackSpace", 8, "\x08") for _ in range(3)]
    assert got == [[kd(8)]] * 3  # Host'ta gerçek klavye gibi tekrarlanan DOWN
    assert rig.m.keys_down == [8]  # bırakma listesinde tek kayıt
    assert rig.up("BackSpace", 8) == [ku(8)]
    assert rig.up("BackSpace", 8) == []  # ikinci UP yok
    assert rig.clean()


def test_modifier_auto_repeat_keeps_order_and_sends_nothing(rig):
    rig.down("Control_L", 17)
    rig.down("Shift_L", 16)
    assert rig.down("Control_L", 17) == [] and rig.down("Shift_L", 16) == []  # Windows tekrar KeyPress yollar
    assert list(rig.m.mods_held.values()) == [CTRL, SHIFT]
    assert rig.down("Escape", 27, "\x1b") == [kd(CTRL), kd(SHIFT), kd(27)]


def test_both_sides_of_a_modifier_share_one_generic_vk(rig):
    rig.down("Shift_L", 16)
    rig.down("Shift_R", 16)
    assert rig.mouse() == [kd(SHIFT)]  # tek genel Shift DOWN
    assert rig.up("Shift_L", 16) == []  # sağ Shift hâlâ basılı
    assert rig.up("Shift_R", 16) == [ku(SHIFT)]
    assert rig.clean()


def test_unknown_releases_send_nothing(rig):
    assert rig.up("a", 65) == []
    assert rig.up("Control_L", 17) == []
    assert rig.up("Left", 37) == []
    assert rig.clean()


def test_unusable_keycodes_are_not_forwarded(rig):
    for keycode in (0, 255, 300, -1):
        assert rig.down("Unidentified", keycode, "") == []
    assert rig.m.keys_down == []
    assert rig.down("ccedilla", 0, "ç") == [c("ç")]  # yazı keycode'a bakmaz


# --- odak kaybı ---

def test_focus_out_releases_every_forwarded_key_and_modifier_exactly_once(rig):
    rig.down("a", 65, "a")  # yazı: hâlâ basılı olsa da bırakma listesinde değil
    rig.down("Control_L", 17)
    rig.down("Shift_L", 16)
    rig.down("Left", 37, "")  # Ctrl, Shift, Left iletildi
    rig.down("F5", 116, "")
    got = rig.focus_out()
    assert sorted(map(repr, got)) == sorted(map(repr, [ku(37), ku(116), ku(CTRL), ku(SHIFT)]))
    assert got == [ku(116), ku(37), ku(SHIFT), ku(CTRL)]  # önce tuşlar, sonra değiştiriciler, ters sırayla
    assert rig.clean()
    assert rig.focus_out() == []  # ikinci çağrı hiçbir şey göndermez
    # Windows odak dönüşünde geç gelen bırakışlar yinelenmez
    for keysym, keycode in (("Left", 37), ("F5", 116), ("Control_L", 17), ("Shift_L", 16), ("a", 65)):
        assert rig.up(keysym, keycode) == []


def test_focus_out_drops_unsent_modifiers_silently_and_forgets_them(rig):
    rig.down("Control_L", 17)  # tembel, hiç iletilmedi
    assert rig.focus_out() == []
    assert rig.clean()
    assert rig.down("a", 65, "a") == [c("a")]  # bayat Ctrl yüzünden kısayol sayılmaz


def test_focus_out_after_modifier_click_releases_the_modifier(rig):
    rig.down("Shift_L", 16)
    rig.mouse()
    assert rig.focus_out() == [ku(SHIFT)]


def test_events_work_normally_after_focus_out(rig):
    rig.down("Left", 37, "")
    rig.focus_out()
    assert rig.down("Left", 37, "") == [kd(37)]
    assert rig.up("Left", 37) == [ku(37)]
    assert rig.clean()


# --- değişmezler: rastgele akışta Host'ta asılı tuş kalmaz ---

POOL = [("Shift_L", 16, ""), ("Shift_R", 16, ""), ("Control_L", 17, ""), ("Control_R", 17, ""), ("Alt_L", 18, ""),
        ("Alt_R", 18, ""), ("Win_L", 91, ""), ("a", 65, "a"), ("A", 65, "A"), ("at", 81, "@"), ("c", 67, "\x03"),
        ("Return", 13, "\r"), ("Left", 37, ""), ("F5", 116, ""), ("Escape", 27, "\x1b"), ("space", 32, " "),
        ("ccedilla", 191, "ç"), ("Delete", 46, "\x7f")]


@pytest.mark.parametrize("seed", range(40))
def test_random_streams_never_leave_keys_stuck_on_the_host(seed):
    rnd = random.Random(seed)
    rig = Rig()
    host_down = set()  # Host'un gözüyle basılı VK'ler
    for _ in range(300):
        action = rnd.random()
        keysym, keycode, ch = rnd.choice(POOL)
        if action < 0.45:
            rig.m.key_press(keysym, keycode, ch)
        elif action < 0.9:
            rig.m.key_release(keysym, keycode)
        elif action < 0.97:
            rig.m.before_mouse()
        else:
            rig.m.focus_out()
            assert rig.clean()
        for ev in rig._take():
            if ev["t"] == "k" and ev["d"] == 1:
                host_down.add(ev["vk"])
            elif ev["t"] == "k":
                assert ev["vk"] in host_down, "DOWN görmeden UP gönderildi"
                host_down.discard(ev["vk"])
        if action >= 0.97:
            assert not host_down, "odak kaybından sonra Host'ta basılı tuş kaldı"
    rig.m.focus_out()
    for ev in rig._take():
        host_down.discard(ev["vk"])
    assert not host_down
