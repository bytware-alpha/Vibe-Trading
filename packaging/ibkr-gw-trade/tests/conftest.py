"""Shared fixtures. Every test gets a private state directory."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from fakes import Runtime


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("IBKR_GW_TRADE_HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    return tmp_path


@pytest.fixture()
def runtime(tmp_path):
    return Runtime(tmp_path)
