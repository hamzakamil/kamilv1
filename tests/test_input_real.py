"""Kabul H, Bölüm 1: Host tarafı gerçek girdi. Yalnız UZAK_REAL_INPUT=1 ile çalışır ve fareyi/klavyeyi GERÇEKTEN kullanır
(tam ekran test penceresine tık, tuş, tekerlek, metin gönderir). Çalışırken fareye ve klavyeye dokunmayın."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.real_input,
    pytest.mark.skipif(os.environ.get("UZAK_REAL_INPUT") != "1", reason="gerçek girdi testi: UZAK_REAL_INPUT=1 ile açılır"),
]


def test_host_injection_into_own_window():
    # Alt süreç: harness takılırsa ya da çökerse test koşucusu da takılı kalmasın (harness ayrıca kendi 20 sn bekçisiyle kapanır)
    r = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "real_input_harness.py")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"çıkış kodu {r.returncode}\n{r.stdout}\n{r.stderr}"
