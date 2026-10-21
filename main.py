"""Kamil V1 giriş noktası (PyInstaller hedefi). Gizli bayrak: --selftest [çıktı dosyası]."""
import sys


class _Skip(Exception):
    pass


# Alt satırlar donmuş exe'nin gerçekten çalıştığını kanıtlar. İçe aktarmalar bilerek statik (PyInstaller görsün).
def _t_imports():
    from uzakmasa import client, config, gui, host, keymap, proto, winio  # noqa: F401


def _t_scrypt():
    from uzakmasa import proto
    key = proto.derive_key("selftest")
    assert len(key) == 32 and key != proto.derive_key("selftest2")


def _t_handshake():
    import socket
    import threading

    from uzakmasa import proto

    key = proto.derive_key("selftest-parolasi-0123456789")

    def pair(client_key):
        a, b = socket.socketpair()
        box = {}

        def serve():
            try:
                box["ch"] = proto.server_handshake(b, "selftest", key)
            except BaseException as e:  # noqa: BLE001
                box["err"] = e

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            cch = proto.client_handshake(a, "selftest", client_key)
            t.join(10)
            assert "ch" in box, box.get("err")
        except BaseException:
            a.close()
            b.close()
            raise
        return a, b, cch, box["ch"]

    a, b, cch, sch = pair(key)
    try:
        cch.send(b"P", b"merhaba")
        assert sch.recv() == (b"P", b"merhaba")
        sch.send(b"P", b"selam")
        assert cch.recv() == (b"P", b"selam")
    finally:
        a.close()
        b.close()
    # yanlış şifre: Host ilk çerçeveyi çözemez, ret sebebi P olarak istemciye ulaşır
    a, b, cch, sch = pair(proto.derive_key("yanlis-parola-xxxxxxxxxx"))
    try:
        cch.send(b"H", b"{}")
        try:
            sch.recv()
        except proto.AuthError:
            pass
        else:
            raise AssertionError("yanlış şifre kabul edildi")
        proto.send_reject(b, b"P")
        try:
            cch.recv()
        except proto.AuthError as e:
            assert e.reason == "P", e.reason
        else:
            raise AssertionError("ret istemciye ulaşmadı")
    finally:
        a.close()
        b.close()


def _t_capture():
    import ctypes

    from uzakmasa import winio

    if ctypes.windll.user32.GetSystemMetrics(0) == 0:
        raise _Skip("ekran yok")
    grabber = winio.Grabber()
    try:
        img, _ = grabber.grab()
    finally:
        getattr(grabber, "close", lambda: None)()
    jpeg = winio.to_jpeg(img, 55, 1280)
    assert img.width > 0 and jpeg[:2] == b"\xff\xd8"
    return f"{img.width}x{img.height}, JPEG {len(jpeg) // 1024} KB"


def _t_tk():
    import tkinter

    from PIL import Image, ImageTk

    root = tkinter.Tk()
    try:
        root.withdraw()
        photo = ImageTk.PhotoImage(Image.new("RGB", (64, 32), (10, 120, 200)), master=root)
        photo.paste(Image.new("RGB", (64, 32), (200, 120, 10)))
        assert (photo.width(), photo.height()) == (64, 32)
        del photo
    finally:
        root.destroy()


def _t_pyngrok():
    from pyngrok import conf, ngrok  # yalnızca içe aktarma: asla bağlanmaz

    assert callable(ngrok.connect) and callable(ngrok.kill) and conf.PyngrokConfig


def _t_input_size():
    import ctypes

    from uzakmasa import winio

    want = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(winio.INPUT) == want, ctypes.sizeof(winio.INPUT)


CHECKS = (("içe aktarma", _t_imports), ("scrypt", _t_scrypt), ("el sıkışma", _t_handshake),
          ("ekran yakalama + JPEG", _t_capture), ("Tk + ImageTk", _t_tk), ("pyngrok", _t_pyngrok),
          ("INPUT boyutu", _t_input_size))


def selftest(out_path=None) -> int:
    """Ağ yok, ngrok yok, girdi enjeksiyonu yok. Sonucu out_path'e yazar; 0 = hepsi geçti."""
    import os
    import tempfile

    try:
        from uzakmasa import winio
        winio.set_dpi_aware()  # mss ve Tk'den önce, gerçek uygulamadaki sıra
    except Exception:  # noqa: BLE001 (içe aktarma hatası zaten "içe aktarma" satırında görünür)
        pass
    lines, failed = [], 0
    for name, fn in CHECKS:
        try:
            note = fn()
            lines.append(f"ok {name}" + (f": {note}" if note else ""))
        except _Skip as s:
            lines.append(f"skip {name}: {s}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
    lines.append("OK" if not failed else f"FAILED: {failed}")  # büyük harfli OK yalnızca tam başarıda geçer
    out_path = out_path or os.path.join(tempfile.gettempdir(), "uzakmasa-selftest.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(selftest(sys.argv[2] if len(sys.argv) > 2 else None))
    from uzakmasa.gui import main
    main()
