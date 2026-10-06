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
from typing import Any

from eeb import canonical
from eeb.cases import rephrase
from eeb.harness.upstreams import Upstream

PROMPT = """Rewrite this question the way a different employee might ask it. Keep its meaning
exactly: the same entities, identifiers, periods, dates, currencies, numbers, comparison
direction and qualifiers, and no assumption about who may see what. Keep every item listed
under "keep verbatim" exactly as written. Reply with the rewritten question only.

Question: {question}

Keep verbatim: {anchors}"""


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
                     only: str = "pending") -> dict[str, int]:
    """Ask for a paraphrase of each selected entry (``pending``: never paraphrased;
    ``rejected``: paraphrased but mechanically rejected). Updates entries in place."""
    if rephrase.family_of(family) == rephrase.family_of(reference_family):
        raise ValueError(f"{family} is the reference agent's family")
    counts = {"asked": 0, "consistent": 0, "rejected": 0, "failed": 0}
    for e in entries:
        done = e.get("rephrased_question")
        prior = (e.get("meaning_preserved_check") or {}).get("mechanical")
        if only == "pending" and done:
            continue
        if only == "rejected" and not (done and prior):
            continue
        body: dict[str, Any] = {"model": model, "max_tokens": 300, "temperature": 0,
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
    return counts


def dumps_queue(entries: list[dict[str, Any]]) -> bytes:
    return canonical.jsonl(entries)
