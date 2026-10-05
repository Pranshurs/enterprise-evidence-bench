"""Artifacts carry no credentials, machine paths or wall-clock values."""

from __future__ import annotations

import datetime as dt
import re

from eeb.generator.core import Config
from eeb.policy import sqlgen


def test_no_credentials_in_artifacts(files: dict[str, bytes]) -> None:
    fixture_password_suffix = sqlgen.login_password("ns", "pid").rsplit("-", 1)[-1].encode()
    needles = [b"eebadmin", b"password", b"PASSWORD", b"postgresql://",
               b"-" + fixture_password_suffix]
    assert [(k, n) for k, v in files.items() for n in needles if n in v] == []


def test_no_wall_clock_dates_in_artifacts(files: dict[str, bytes]) -> None:
    today = dt.date.today()
    cfg = Config(seed=7)
    assert today > cfg.today or today < cfg.start  # guard: real today is outside the window
    stamp = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    assert [k for k, v in files.items() if stamp.search(v)] == []
    iso_today = today.isoformat().encode()
    assert [k for k, v in files.items() if iso_today in v] == []
