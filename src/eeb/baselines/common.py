"""Shared parts of the framework-free baselines B1–B3 (ADR-0009).

Everything a baseline does is here and visible: a naive paragraph chunker over the rendered
documents, BM25 retrieval over the full corpus, one model client that goes through the
harness gateway, the output protocol the model is asked to follow, and the mapping of its
answer onto the §8 response. The experimental controls are constants, shared by B1–B3.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# Frozen experimental controls (ADR-0009). The model id is set per run.
TEMPERATURE = 0
MAX_TOKENS = 1024
TOP_K = 5
RETRIES = 0
SQL_ROW_CAP = 200
SQL_TIMEOUT = "5s"
MAX_QUERIES = 3

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Passage:
    doc_id: str
    version: int
    start: int
    end: int
    text: str


def chunk_documents(documents_dir: Path) -> list[Passage]:
    """Paragraphs (blank-line separated) of every rendered document, with exact offsets.
    The file name ``<doc_id>@v<version>.md`` gives the document and version."""
    out: list[Passage] = []
    for path in sorted(documents_dir.glob("*@v*.md")):
        doc_id, version = path.stem.rsplit("@v", 1)
        text = path.read_text("utf-8")
        for m in re.finditer(r"[^\n](?:.|\n(?!\n))*", text):
            if m.group(0).strip():
                out.append(Passage(doc_id, int(version), m.start(), m.end(), m.group(0)))
    return out


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class BM25:
    """Okapi BM25 (k1 = 1.5, b = 0.75) over passages; ties broken by corpus order."""

    def __init__(self, passages: list[Passage], k1: float = 1.5, b: float = 0.75) -> None:
        self.passages = passages
        self.docs = [Counter(tokens(p.text)) for p in passages]
        self.len = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.len) / max(1, len(self.len))
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(passages)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def top(self, query: str, k: int = TOP_K) -> list[Passage]:
        q = set(tokens(query))
        scored = []
        for i, (d, n) in enumerate(zip(self.docs, self.len, strict=True)):
            s = 0.0
            for t in q:
                f = d.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * n / self.avg))
            if s > 0:
                scored.append((-s, i))
        return [self.passages[i] for _, i in sorted(scored)[:k]]


class Model:
    """Chat completions through the harness gateway (OpenAI-compatible), one call per
    request step, no retries."""

    def __init__(self, gateway_url: str, model_id: str) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.model_id = model_id

    def chat(self, request_id: str, system: str, user: str) -> str:
        body = {"model": self.model_id, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        r = httpx.post(f"{self.gateway_url}/r/{request_id}/v1/chat/completions", json=body,
                       headers={"x-bench-request-id": request_id}, timeout=120)
        r.raise_for_status()
        return str(r.json()["choices"][0]["message"]["content"])


ANSWER_PROTOCOL = """Reply with one JSON object and nothing else:
{"outcome": "ANSWER" | "ABSTAIN" | "CLARIFY",
 "answer_text": "...",
 "claims": [{"text": "...", "value": "...", "unit": "...",
             "sources": ["P1", "Q1:0:column", ...]}],
 "clarify": {"axis": "...", "options": ["..."]} | null}
Cite passages as P<n> and query results as Q<n>:<row>:<column>."""


def passages_block(passages: list[Passage]) -> str:
    return "\n\n".join(f"[P{i + 1}] ({p.doc_id} v{p.version})\n{p.text}"
                       for i, p in enumerate(passages))


def parse_json(text: str) -> dict[str, Any] | None:
    """The first JSON object in a model reply, or None."""
    start = text.find("{")
    while start >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def to_response(reply: str, passages: list[Passage], receipts: list[dict[str, Any]],
                trace: dict[str, Any]) -> dict[str, Any]:
    """Map the model's reply onto the §8 response. A reply that is not the protocol's JSON
    is returned as an answer with no structured claims (what a naive system shows)."""
    obj = parse_json(reply)
    resp: dict[str, Any] = {"outcome": "ANSWER", "answer_text": reply, "claims": [],
                            "conflicts": [], "clarify": None,
                            "sql_receipts": [r["public"] for r in receipts],
                            "user_trace": trace}
    if obj is None:
        return resp
    outcome = obj.get("outcome")
    resp["outcome"] = outcome if outcome in ("ANSWER", "ABSTAIN", "CLARIFY") else "ANSWER"
    resp["answer_text"] = str(obj.get("answer_text", ""))
    if resp["outcome"] == "CLARIFY":
        cq = obj.get("clarify")
        resp["clarify"] = cq if isinstance(cq, dict) and isinstance(cq.get("axis"), str) \
            and isinstance(cq.get("options"), list) else {"axis": "", "options": []}
    for cl in obj.get("claims", []) if isinstance(obj.get("claims"), list) else []:
        if not isinstance(cl, dict):
            continue
        cits = []
        for ref in cl.get("sources", []) if isinstance(cl.get("sources"), list) else []:
            c = _citation(str(ref), passages, receipts)
            if c is not None:
                cits.append(c)
        claim = {"text": str(cl.get("text", "")), "citations": cits}
        for k in ("value", "unit"):
            if cl.get(k) is not None:
                claim[k] = cl[k]
        resp["claims"].append(claim)
    return resp


def _citation(ref: str, passages: list[Passage], receipts: list[dict[str, Any]]
              ) -> dict[str, Any] | None:
    m = re.fullmatch(r"P(\d+)", ref.strip())
    if m and 1 <= int(m.group(1)) <= len(passages):
        p = passages[int(m.group(1)) - 1]
        return {"kind": "doc", "doc_id": p.doc_id, "version": p.version, "start": p.start,
                "end": p.end}
    m = re.fullmatch(r"Q(\d+):(\d+):(\w+)", ref.strip())
    if m and 1 <= int(m.group(1)) <= len(receipts):
        rec = receipts[int(m.group(1)) - 1]["public"]
        return {"kind": "sql", "receipt_id": rec["receipt_id"], "rows": [int(m.group(2))],
                "columns": [m.group(3)]}
    return None
