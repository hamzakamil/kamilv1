"""Ayarlar: %LOCALAPPDATA%\\Kamil V1\\config.json (utf-8)."""
import json
import os
import re
import secrets
import threading

# Derleyen doldurabilir. DEFAULT_NGROK_TOKEN exe'den çıkarılabilir: bilen herkes o ngrok hesabını kullanır.
DEFAULT_RELAY = ""
DEFAULT_RELAY_TOKEN = ""
DEFAULT_NGROK_TOKEN = ""
DEFAULT_BORE_SERVER = "bore.pub"  # hesapsız, ücretsiz herkese açık tünel sunucusu; "sunucu" ya da "sunucu:kontrol_portu"

APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Kamil V1")

_FORMATS = {"uid": r"[0-9a-f]{32}", "relay_id": r"[0-9]{9}"}
_lock = threading.RLock()  # load() kilidi tutarken save() çağırır


def load() -> dict:
    with _lock:
        path = os.path.join(APPDIR, "config.json")
        cfg = {}
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("kök nesne sözlük değil")
        except FileNotFoundError:
            pass
        except ValueError:  # bozuk JSON/utf-8: kopyasını sakla, varsayılanla başla
            cfg = {}
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass
        defaults = {
            "uid": secrets.token_hex(16),
            "mode": "bore",
            "bore_server": DEFAULT_BORE_SERVER,
            "bore_port": 0,  # son verilen genel port: aynı portu isteyerek ID'yi kararlı tutmayı dener
            "ngrok_token": DEFAULT_NGROK_TOKEN,
            "relay": DEFAULT_RELAY,
            "relay_token": DEFAULT_RELAY_TOKEN,
            "relay_id": "%09d" % secrets.randbelow(10**9),
            "k": "",
            "trusted": [],
        }
        changed = False
        for key, default in defaults.items():
            value = cfg.get(key)
            if not isinstance(value, type(default)) or (key in _FORMATS and not re.fullmatch(_FORMATS[key], value)):
                cfg[key] = default
                changed = True
        if changed:  # üretilen uid/relay_id kalıcı olmalı, yoksa her açılışta değişir
            try:
                save(cfg)
            except OSError:
                pass  # shortcut: yazılamayan profilde kimlik yalnızca bu oturumluk, uyarı yok, yükseltme: arayüzde uyar
        return cfg


# shortcut: diskte şifreleme/DPAPI yok (k, uid, trusted kimlik bilgisidir), aynı kullanıcının zararlısı zaten okur, yükseltme: DPAPI
def save(cfg: dict) -> None:
    with _lock:
        data = json.dumps(cfg, ensure_ascii=False, indent=2)  # önce serileştir: hata olursa diske dokunulmaz
        os.makedirs(APPDIR, exist_ok=True)
        path = os.path.join(APPDIR, "config.json")
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
