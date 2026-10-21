"""Proje kökünü sys.path'e koyar; real_input testleri yalnızca UZAK_REAL_INPUT=1 ile çalışır."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("UZAK_REAL_INPUT") == "1":
        return
    skip = pytest.mark.skip(reason="gerçek fare/klavye girdisi gönderir; UZAK_REAL_INPUT=1 ile açılır")
    for item in items:
        if item.get_closest_marker("real_input"):
            item.add_marker(skip)
