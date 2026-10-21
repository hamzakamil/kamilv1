"""Görüntüleyici tarafı klavye mantığı (tkinter yok): Tk tuş olaylarını Host'a giden olaylara çevirir.

Yazı (ş ğ ı İ @ € ...) Host'un düzeninden bağımsız 'c' olayıyla gider. Ham tuş ('k', Windows VK) yalnızca yazıya
dönüşmeyen tuşlar (Enter, oklar, F tuşları...) ve kısayollar içindir. Değiştirici tuşlar tembeldir: basışları ancak
bir 'k' ya da fare olayı onlara ihtiyaç duyunca iletilir. Böylece AltGr+q Host'ta Ctrl+Alt basılı bırakmaz, tek
başına Shift/Alt vuruşu hiçbir şey göndermez.
"""

CTRL, ALT = 0x11, 0x12
# keysym -> genel VK. AltGr, Windows'ta Control_L + Alt_R olarak gelir. Tk-Windows Win tuşuna Win_L/Win_R der
# (event_generate keycode=91 ile doğrulandı); Super_* başka Tk sürümleri için.
_MODS = {
    "Shift_L": 0x10, "Shift_R": 0x10,
    "Control_L": CTRL, "Control_R": CTRL,
    "Alt_L": ALT, "Alt_R": ALT,
    "Win_L": 0x5B, "Win_R": 0x5B, "Super_L": 0x5B, "Super_R": 0x5B,
}


class KeyMapper:
    def __init__(self, send):
        self.send = send      # Client.send_input: sözlük alır, bloklamaz
        self.mods_held = {}   # basılı değiştirici keysym -> genel VK (basış sırasıyla)
        self.sent_mods = []   # DOWN'u Host'a iletilmiş genel VK'ler
        self.keys_down = []   # DOWN'u Host'a iletilmiş normal tuşların VK'si

    def key_press(self, keysym, keycode, char):
        vk = _MODS.get(keysym)
        if vk:
            self.mods_held.setdefault(keysym, vk)  # otomatik tekrar basış sırasını bozmasın
            return
        held = self.mods_held.values()
        # AltGr (Ctrl+Alt birlikte) yazıdır; tek başına Ctrl ya da Alt kısayoldur
        if len(char) == 1 and char.isprintable() and (CTRL in held) == (ALT in held):
            self.send({"t": "c", "c": char})  # basılı tutulan tuş tekrarlanan KeyPress = tekrarlanan karakter
            return
        if not 1 <= keycode <= 254:  # Host zaten reddeder; bırakma listesini de kirletmesin
            return
        self._flush()
        if keycode not in self.keys_down:
            self.keys_down.append(keycode)
        self.send({"t": "k", "vk": keycode, "d": 1})  # tekrarlanan basış = Host'ta otomatik tekrar

    def key_release(self, keysym, keycode):
        vk = _MODS.get(keysym)
        if vk:
            self.mods_held.pop(keysym, None)
            # iki taraf birden basılıysa (Shift_L + Shift_R) genel VK son bırakışta kalkar
            if vk in self.sent_mods and vk not in self.mods_held.values():
                self.sent_mods.remove(vk)
                self.send({"t": "k", "vk": vk, "d": 0})
        elif keycode in self.keys_down:  # yazı tuşları ('c') hiçbir şey göndermez
            self.keys_down.remove(keycode)
            self.send({"t": "k", "vk": keycode, "d": 0})

    def before_mouse(self):
        """Ctrl+tık, Shift+tık için: görüntüleyici her düğme/tekerlek olayından önce çağırır."""
        self._flush()

    def focus_out(self):
        """Odak kaybı ya da pencere kapanışı: Host'ta basılı tuş kalmasın. İletilenlerin her biri tam bir kez bırakılır."""
        for vk in (*reversed(self.keys_down), *reversed(self.sent_mods)):
            self.send({"t": "k", "vk": vk, "d": 0})
        self.mods_held.clear()
        self.sent_mods.clear()
        self.keys_down.clear()

    def _flush(self):
        for vk in self.mods_held.values():
            if vk not in self.sent_mods:
                self.sent_mods.append(vk)
                self.send({"t": "k", "vk": vk, "d": 1})
