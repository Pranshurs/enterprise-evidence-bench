"""Scorers on real receipts: the gold-perfect response's SQL is run as a SUT would run it
(the principal's own login), the harness re-executes every receipt under the principal's
verifier twin (§9.3), and the scorer judges SQL citations on the harness's rows only.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import psycopg
import pytest

from eeb.cases.build import CaseBuilder
from eeb.cases.view import InstanceData
from eeb.db.load import admin, build_database, drop, dsn_for, read_jsonl
from eeb.harness.receipts import cursor_digest, verify_receipt
from eeb.policy import sqlgen
from eeb.scoring.evidence import Evidence
from eeb.scoring.ideal import ideal_response
from eeb.scoring.score import Observed, ReceiptVerdict, score_case
from tests.conftest import SEED, unique_ns
from tests.test_case_build import PROBE_SHARE_ON_SMALL, SLOTS_ON_SMALL
from tests.test_scoring import perfect_problems

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def data(instance_dir: Path) -> InstanceData:
    return InstanceData.load(instance_dir)


@pytest.fixture(scope="module")
def cases(data: InstanceData, instance_dir: Path) -> list[dict[str, Any]]:
    full = read_jsonl(instance_dir / "cases/plan.jsonl")
    keep: set[str] = set()
    for cls, n in SLOTS_ON_SMALL.items():
        keep |= set(sorted(s["case_id"] for s in full if s["class"] == cls)[:n])
    plan = [s for s in full if s["class"] not in SLOTS_ON_SMALL or s["case_id"] in keep]
    return CaseBuilder(data, SEED, PROBE_SHARE_ON_SMALL).build(plan)


@pytest.fixture(scope="module")
def db(pg_dsn: str, instance_dir: Path) -> Any:
    ns = unique_ns("score")
    build_database(pg_dsn, instance_dir, ns)
    yield ns
    drop(pg_dsn, ns)


def _sut_digest(pg_dsn: str, ns: str, pid: str, sql: str) -> str:
    """What a SUT computes for its receipt: the published digest of its own result."""
    with psycopg.connect(dsn_for(pg_dsn, ns, sqlgen.login_role(ns, pid),
                                 sqlgen.login_password(ns, pid))) as conn:
        cur = conn.execute(sql)
        return cursor_digest(cur, cur.fetchall())


def _harness(pg_dsn: str, ns: str, case: dict[str, Any], resp: dict[str, Any]) -> Observed:
    obs = Observed()
    for r in resp["sql_receipts"]:
        chk = verify_receipt(pg_dsn, ns, case["principal_id"], r, "P", [])
        obs.receipts[r["receipt_id"]] = ReceiptVerdict(chk.status, chk.columns, chk.rows)
    return obs


def _with_receipts(pg_dsn: str, ns: str, case: dict[str, Any]) -> dict[str, Any]:
    r = ideal_response(case)
    for rec in r["sql_receipts"]:
        rec["result_digest"] = _sut_digest(pg_dsn, ns, case["principal_id"], rec["sql"])
    return r


def _point_citations_at(resp: dict[str, Any], obs: Observed) -> None:
    """The ideal response names its single result column ``value``; cite the column the
    query actually returns."""
    for cl in resp["claims"]:
        for c in cl["citations"]:
            if c["kind"] == "sql" and obs.receipts[c["receipt_id"]].columns:
                c["columns"] = [obs.receipts[c["receipt_id"]].columns[0]]
    for cf in resp["conflicts"]:
        for c in cf["evidence"]:
            if c["kind"] == "sql" and obs.receipts[c["receipt_id"]].columns:
                c["columns"] = [obs.receipts[c["receipt_id"]].columns[0]]


def test_clean_arm_on_receipts_the_harness_re_executes(pg_dsn: str, db: str, data: InstanceData,
                                                       cases: list[dict[str, Any]]) -> None:
    ev = Evidence(data)
    bad, receipts = {}, 0
    for c in cases:
        r = _with_receipts(pg_dsn, db, c)
        obs = _harness(pg_dsn, db, c, r)
        receipts += len(obs.receipts)
        assert all(v.status == "verified" for v in obs.receipts.values()), c["case_id"]
        _point_citations_at(r, obs)
        probs = perfect_problems(score_case(c, r, ev, obs))
        if probs:
            bad[c["case_id"]] = probs
    assert bad == {}
    assert receipts > 50


def test_red_arm_receipt_computed_with_wider_rights_is_not_verified(
        pg_dsn: str, db: str, data: InstanceData, cases: list[dict[str, Any]]) -> None:
    """A restricted-probe case answered from all rows: the SUT's digest comes from the
    administrator's result, the harness's re-execution under the asker differs."""
    c = next(c for c in cases if c["restricted_probe"] and any(
        f["source"] == "sql" for f in c["gold_facts"]))
    r = ideal_response(c)
    with admin(pg_dsn, db) as root:
        for rec in r["sql_receipts"]:
            cur = root.execute(rec["sql"])
            rec["result_digest"] = cursor_digest(cur, cur.fetchall())
    obs = _harness(pg_dsn, db, c, r)
    assert {v.status for v in obs.receipts.values()} == {"digest_mismatch"}
    assert all(v.rows == [] for v in obs.receipts.values())
    s = score_case(c, r, Evidence(data), obs)
    assert s["sql"]["execution_correct"] == 0
    assert any("digest_mismatch" in p for i in s["citations"]["invalid"] for p in i["problems"])


def test_red_arm_reported_rows_are_not_trusted(pg_dsn: str, db: str, data: InstanceData,
                                               cases: list[dict[str, Any]]) -> None:
    """The SUT's own ``rows`` claim the right value, but its query returns something else:
    the citation is judged on what the harness re-executed."""
    c = next(c for c in cases if c["expected_outcome"] == "ANSWER" and any(
        cit["kind"] == "sql" for cl in ideal_response(c)["claims"] for cit in cl["citations"]))
    r = _with_receipts(pg_dsn, db, c)
    bad = copy.deepcopy(r)
    for rec in bad["sql_receipts"]:
        rec["sql"] = f"SELECT -1 AS value FROM ({rec['sql']}) AS g"
        rec["result_digest"] = _sut_digest(pg_dsn, db, c["principal_id"], rec["sql"])
    obs = _harness(pg_dsn, db, c, bad)
    assert {v.status for v in obs.receipts.values()} == {"verified"}
    _point_citations_at(bad, obs)
    s = score_case(c, bad, Evidence(data), obs)
    assert s["sql"]["gold_sql_facts"] >= 1 and s["sql"]["execution_correct"] == 0
    assert any("do not hold the value" in p for i in s["citations"]["invalid"]
               for p in i["problems"])
