from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eeb.db.load import _parse, build_database, drop, parsed_assignments, read_jsonl
from eeb.generator.core import Config
from eeb.generator.instance import build_instance_files, write_files
from eeb.policy import schema as pschema
from eeb.policy.oracle import Oracle
from eeb.schema import TABLES

SEED = 7


@pytest.fixture(scope="session")
def files() -> dict[str, bytes]:
    return build_instance_files(Config(seed=SEED))


@pytest.fixture(scope="session")
def instance_dir(files: dict[str, bytes], tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("instance") / f"seed{SEED}"
    write_files(files, out)
    return out


@pytest.fixture(scope="session")
def tables(instance_dir: Path) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for t in TABLES:
        recs = read_jsonl(instance_dir / f"tables/{t.name}.jsonl")
        out[t.name] = [dict(zip(t.column_names, _parse(t, r), strict=True)) for r in recs]
    return out


@pytest.fixture(scope="session")
def oracle(instance_dir: Path, tables: dict[str, list[dict[str, Any]]]) -> Oracle:
    policy = pschema.load_policy((instance_dir / "policy.yaml").read_bytes())
    return Oracle(policy, tables, parsed_assignments(instance_dir), dt.date(2026, 7, 15))


def _admin_dsn() -> str | None:
    return os.environ.get("EEB_PG_ADMIN_DSN")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = _admin_dsn()
    if not dsn:
        if os.environ.get("EEB_REQUIRE_PG") == "1":
            pytest.fail("EEB_REQUIRE_PG=1 but EEB_PG_ADMIN_DSN is not set")
        pytest.skip("Postgres fixture not configured (EEB_PG_ADMIN_DSN)")
    return dsn


def unique_ns(tag: str) -> str:
    return f"eebt_{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def built_db(pg_dsn: str, instance_dir: Path) -> Iterator[str]:
    ns = unique_ns("main")
    build_database(pg_dsn, instance_dir, ns)
    yield ns
    drop(pg_dsn, ns)
