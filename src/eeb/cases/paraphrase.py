"""Fill the rephrase queue with paraphrases from a model family independent of the
reference agent's (spec §7; ADR-0009: reference agent OpenAI, so Anthropic or Gemini).

The model sees the canonical question and its ``must_preserve`` anchors only, never gold
values, evidence or principals. Every paraphrase is checked mechanically against the
canonical wording (``rephrase.check``) and the result is recorded next to it; a rejected
paraphrase stays in the queue as rejected and can be asked for again. One attempt per
question per call: nothing is retried until it happens to pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from eeb import canonical
from eeb.cases import rephrase
from eeb.harness.upstreams import Upstream

# Generation parameters, recorded in the run's provenance.
MAX_TOKENS = 300
TEMPERATURE = 0

PROMPT = """Rewrite this question the way a different employee might ask it. Keep its meaning
exactly: the same entities, identifiers, periods, dates, currencies, numbers, comparison
direction and qualifiers, and no assumption about who may see what. Keep every item listed
under "keep verbatim" exactly as written. Reply with the rewritten question only.

Question: {question}

Keep verbatim: {anchors}"""


@dataclass
class ParaphraseRun:
    """Counts, and each paraphrase produced (family id, model tag, text digest): what the
    provenance record binds and the freeze checks."""
    counts: dict[str, int]
    produced: list[dict[str, str]]


def _anchors(entry: dict[str, Any]) -> str:
    mp = entry["must_preserve"]
    items = [x for k in ("suppliers", "identifiers", "periods", "dates", "currencies",
                         "numbers") for x in mp.get(k, [])]
    return "; ".join(items) if items else "(none)"


def prompt_for(entry: dict[str, Any]) -> str:
    return PROMPT.format(question=entry["question_canonical"], anchors=_anchors(entry))


def _text(api: str, resp: dict[str, Any]) -> str:
    if api == "anthropic.messages":
        return "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text").strip()
    return str(resp["choices"][0]["message"]["content"]).strip()


def paraphrase_queue(entries: list[dict[str, Any]], upstream: Upstream, api: str,
                     model: str, family: str, reference_family: str,
                     only: str = "pending", limit: int = 0) -> ParaphraseRun:
    """Ask for a paraphrase of each selected entry (``pending``: never paraphrased;
    ``rejected``: paraphrased but mechanically rejected), at most ``limit`` of them when
    set (a pilot). Updates entries in place."""
    if rephrase.family_of(family) == rephrase.family_of(reference_family):
        raise ValueError(f"{family} is the reference agent's family")
    counts = {"asked": 0, "consistent": 0, "rejected": 0, "failed": 0}
    produced: list[dict[str, str]] = []
    for e in entries:
        done = e.get("rephrased_question")
        prior = (e.get("meaning_preserved_check") or {}).get("mechanical")
        if only == "pending" and done:
            continue
        if only == "rejected" and not (done and prior):
            continue
        if limit and counts["asked"] >= limit:
            break
        body: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS,
                                "temperature": TEMPERATURE,
                                "messages": [{"role": "user", "content": prompt_for(e)}]}
        counts["asked"] += 1
        status, resp, _ = asyncio.run(upstream.complete(api, body))
        if status != 200:
            counts["failed"] += 1
            continue
        text = _text(api, resp)
        tag = f"{rephrase.family_of(family)}:{model}"
        problems = rephrase.check(e["question_canonical"], text, tag, reference_family)
        e.update(rephrased_question=text, rephrased_by_model_family=tag,
                 meaning_preserved_check={"mechanical": problems, "human": None})
        counts["rejected" if problems else "consistent"] += 1
        produced.append({"family_id": e["family_id"], "model": tag,
                         "rephrased_sha256": canonical.sha256_text(text)})
    return ParaphraseRun(counts, produced)


def provenance_record(model: str, family: str, api: str, only: str, limit: int,
                      run: ParaphraseRun, commit: str, when: str) -> dict[str, Any]:
    """One paraphrase run, for ``rephrase_provenance.jsonl``. Only what reproduces the
    request: never keys, headers or raw provider responses."""
    return {"when": when, "commit": commit, "family": family, "model": model, "api": api,
            "parameters": {"temperature": TEMPERATURE, "max_tokens": MAX_TOKENS},
            "prompt_sha256": canonical.sha256_text(PROMPT), "selection": only,
            "limit": limit or None, "counts": run.counts, "produced": run.produced}


REQUIRED_RUN_KEYS = ("commit", "family", "model", "api", "parameters", "prompt_sha256",
                     "counts", "produced")


def provenance_problems(queue: list[dict[str, Any]], runs: list[Any] | None) -> list[str]:
    """Why the provenance does not bind the queue's paraphrases (empty: it does). Every
    paraphrased entry must have been produced, with exactly its text and model, by a
    well-formed run; a missing, empty or malformed record binds nothing."""
    paraphrased = [e for e in queue if e.get("rephrased_question")]
    if not paraphrased:
        return []
    if runs is None:
        return ["rephrase provenance is missing"]
    if not runs:
        return ["rephrase provenance is empty"]
    out: list[str] = []
    bound: set[tuple[str, str, str]] = set()
    for i, r in enumerate(runs):
        if not isinstance(r, dict) or any(k not in r for k in REQUIRED_RUN_KEYS) or \
                not isinstance(r["produced"], list):
            out.append(f"rephrase provenance run {i} is malformed")
            continue
        for p in r["produced"]:
            if isinstance(p, dict) and all(isinstance(p.get(k), str) for k in
                                           ("family_id", "model", "rephrased_sha256")):
                if p["model"] != f"{r['family']}:{r['model']}":
                    out.append(f"rephrase provenance run {i} names a model it did not run")
                bound.add((p["family_id"], p["model"], p["rephrased_sha256"]))
            else:
                out.append(f"rephrase provenance run {i} has a malformed entry")
    for e in paraphrased:
        key = (e["family_id"], str(e.get("rephrased_by_model_family")),
               canonical.sha256_text(str(e["rephrased_question"])))
        if key not in bound:
            out.append(f"paraphrase of {e['family_id']} is not bound by the provenance")
    return out


def dumps_queue(entries: list[dict[str, Any]]) -> bytes:
    return canonical.jsonl(entries)
