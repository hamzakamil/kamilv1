"""config.py testleri: varsayılanlar, atomik kayıt, bozuk dosya, utf-8, APPDIR."""
import importlib.util
import json
import os
import re
import threading

import pytest

from uzakmasa import config

CFG = "config.json"
TR = "Şifre: ğüşiöçı İĞÜŞÖÇ @€ — 'ç'"


@pytest.fixture(autouse=True)
def appdir(tmp_path, monkeypatch):
    """Gerçek profile asla dokunma: LOCALAPPDATA ve APPDIR tmp_path'e yönlenir."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = tmp_path / "Kamil V1"
    monkeypatch.setattr(config, "APPDIR", str(d))
    return d


def read_raw(d):
    return json.loads((d / CFG).read_text(encoding="utf-8"))


def names(d):
    return sorted(p.name for p in d.iterdir())


def test_defaults_generated_once_and_stable_across_loads(appdir):
    assert not appdir.exists()
    a = config.load()
    assert re.fullmatch(r"[0-9a-f]{32}", a["uid"])
    assert re.fullmatch(r"[0-9]{9}", a["relay_id"])
    assert a["mode"] == "ngrok" and a["k"] == "" and a["trusted"] == []
    assert a["ngrok_token"] == config.DEFAULT_NGROK_TOKEN
    assert a["relay"] == config.DEFAULT_RELAY and a["relay_token"] == config.DEFAULT_RELAY_TOKEN
    assert read_raw(appdir) == a  # üretilen kimlikler kalıcı olmalı
    assert config.load() == a == config.load()


def test_each_install_gets_its_own_uid(tmp_path, monkeypatch):
    first = config.load()
    monkeypatch.setattr(config, "APPDIR", str(tmp_path / "baska"))
    assert config.load()["uid"] != first["uid"]


def test_missing_keys_filled_and_unknown_keys_kept(appdir):
    appdir.mkdir()
    uid = "ab" * 16
    (appdir / CFG).write_text(json.dumps({"uid": uid, "mode": "relay", "ileride": [1, 2]}), encoding="utf-8")
    cfg = config.load()
    assert cfg["uid"] == uid and cfg["mode"] == "relay" and cfg["ileride"] == [1, 2]
    assert re.fullmatch(r"[0-9]{9}", cfg["relay_id"]) and cfg["trusted"] == []
    assert read_raw(appdir) == cfg  # tamamlanan alanlar kalıcı
    assert config.load() == cfg


def test_wrong_types_and_formats_replaced_with_defaults(appdir):
    appdir.mkdir()
    bad = {"uid": "NOTHEX", "relay_id": 12345, "trusted": "abc", "mode": 3, "k": None, "relay": ["x"], "ngrok_token": 5}
    (appdir / CFG).write_text(json.dumps(bad), encoding="utf-8")
    cfg = config.load()
    assert re.fullmatch(r"[0-9a-f]{32}", cfg["uid"]) and re.fullmatch(r"[0-9]{9}", cfg["relay_id"])
    assert cfg["trusted"] == [] and cfg["mode"] == "ngrok" and cfg["k"] == ""
    assert cfg["relay"] == config.DEFAULT_RELAY and cfg["ngrok_token"] == config.DEFAULT_NGROK_TOKEN
    # biçimi tutan uid korunur, 9 haneli olmayan relay_id yenilenir
    (appdir / CFG).write_text(json.dumps({"uid": "a" * 32, "relay_id": "12345678"}), encoding="utf-8")
    cfg = config.load()
    assert cfg["uid"] == "a" * 32 and cfg["relay_id"] != "12345678" and len(cfg["relay_id"]) == 9


@pytest.mark.parametrize("raw", [b"{bu json degil", b"", b"[1, 2]", b'"yazi"', b"\xfe\xff\x00 cp1254 \xde"])
def test_corrupt_file_kept_as_bad_and_defaults_used(appdir, raw):
    appdir.mkdir()
    (appdir / CFG).write_bytes(raw)
    cfg = config.load()
    assert (appdir / (CFG + ".bad")).read_bytes() == raw
    assert re.fullmatch(r"[0-9a-f]{32}", cfg["uid"])
    assert read_raw(appdir) == cfg  # yerine yeni, geçerli dosya yazıldı
    assert config.load() == cfg  # sonraki yüklemeler kararlı
    assert (appdir / (CFG + ".bad")).read_bytes() == raw  # .bad yeniden üretilmez


def test_unreadable_config_is_not_overwritten(appdir):
    (appdir / CFG).mkdir(parents=True)  # okunamayan "dosya": kullanıcının verisinin üstüne yazılmamalı
    with pytest.raises(OSError):
        config.load()
    assert (appdir / CFG).is_dir()


def test_load_survives_unwritable_profile(appdir, monkeypatch):
    def boom(cfg):
        raise OSError("salt okunur")

    monkeypatch.setattr(config, "save", boom)
    assert re.fullmatch(r"[0-9a-f]{32}", config.load()["uid"])
    assert not appdir.exists()


def test_utf8_roundtrip_turkish_text(appdir):
    cfg = config.load()
    cfg["relay_token"] = TR
    cfg["trusted"] = [TR, "b"]
    config.save(cfg)
    raw = (appdir / CFG).read_bytes()
    assert TR.encode("utf-8") in raw and b"\\u" not in raw  # gerçek utf-8, \u kaçışı değil
    assert config.load() == cfg and config.load()["relay_token"] == TR


def test_load_reads_utf8_regardless_of_locale_encoding(appdir):
    appdir.mkdir()  # bu makinede varsayılan kodlama cp1254; 'Ğ' gibi karakterler onunla çözülemezdi
    (appdir / CFG).write_bytes(json.dumps({"relay_token": TR}, ensure_ascii=False).encode("utf-8"))
    assert config.load()["relay_token"] == TR


def test_save_is_complete_and_leaves_no_temp_files(appdir):
    cfg = config.load()
    cfg["mode"] = "relay"
    config.save(cfg)
    assert read_raw(appdir)["mode"] == "relay"
    assert names(appdir) == [CFG]


def test_failed_save_leaves_previous_file_intact(appdir, monkeypatch):
    cfg = config.load()
    before = (appdir / CFG).read_bytes()
    with pytest.raises(TypeError):  # serileştirme hatası diske hiç dokunmaz
        config.save({"küme": {1, 2}})
    assert (appdir / CFG).read_bytes() == before and names(appdir) == [CFG]  # artık .tmp da kalmaz
    with monkeypatch.context() as m:  # yazma bitti ama değiştirme patladı: eski dosya olduğu gibi kalır
        def dying_replace(src, dst):
            raise OSError("disk dolu")

        m.setattr(config.os, "replace", dying_replace)
        with pytest.raises(OSError):
            config.save({**cfg, "mode": "relay"})
    assert (appdir / CFG).read_bytes() == before
    assert config.load() == cfg


def test_concurrent_saves_and_loads_never_see_a_torn_file(appdir):
    config.load()
    errors = []

    def writer(i):
        try:
            for j in range(25):
                cfg = config.load()
                cfg["trusted"] = ["%d-%d" % (i, j)] * 50
                config.save(cfg)
        except BaseException as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                assert isinstance(config.load()["trusted"], list)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + [threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert names(appdir) == [CFG]  # .bad ya da artık .tmp yok


def _fresh_import():
    spec = importlib.util.spec_from_file_location("config_kopya", config.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # asıl modüle dokunmadan taze içe aktarma
    return mod


def test_appdir_is_under_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    fresh = _fresh_import()
    assert fresh.APPDIR == os.path.join(str(tmp_path / "Local"), "Kamil V1")
    assert not os.path.exists(fresh.APPDIR)  # içe aktarma klasör oluşturmaz


def test_appdir_falls_back_to_home_when_localappdata_missing(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA")
    fresh = _fresh_import()
    assert fresh.APPDIR == os.path.join(os.path.expanduser("~"), "Kamil V1")
    monkeypatch.setenv("LOCALAPPDATA", "")  # boş değer göreli yol üretmemeli
    assert os.path.isabs(_fresh_import().APPDIR)
