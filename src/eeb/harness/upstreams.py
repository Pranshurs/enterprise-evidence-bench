"""Model upstreams behind the gateway (ADR-0005 §4).

An upstream receives the parsed request body and returns ``(status, response_json,
usage)``. Credentials are injected here and only here: the SUT never holds them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None


class Upstream(Protocol):
    name: str

    async def complete(self, api: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], Usage]:
        ...


def _words(body: dict[str, Any]) -> int:
    from eeb.harness.text import all_strings

    return sum(len(s.split()) for s in all_strings(body))


@dataclass
class ScriptedUpstream:
    """Deterministic offline model. ``script`` maps a request to reply text; the default
    reply is a fixed string, so offline runs are reproducible. Usage counts are word counts
    (labelled as such in reports; they are not provider tokens)."""

    script: Callable[[str, dict[str, Any]], str] | None = None
    name: str = "scripted"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(self, api: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], Usage]:
        self.calls.append(body)
        text = self.script(api, body) if self.script else "SCRIPTED-REPLY"
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        usage = Usage(_words(body), len(text.split()))
        if api == "anthropic.messages":
            resp = {"id": f"msg_scripted_{digest}", "type": "message", "role": "assistant",
                    "model": body.get("model", "scripted"),
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": usage.input_tokens,
                              "output_tokens": usage.output_tokens}}
        elif api == "openai.embeddings":
            inputs = body.get("input", [])
            n = len(inputs) if isinstance(inputs, list) else 1
            resp = {"object": "list", "model": body.get("model", "scripted"),
                    "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 8}
                             for i in range(n)],
                    "usage": {"prompt_tokens": usage.input_tokens,
                              "total_tokens": usage.input_tokens}}
            usage = Usage(usage.input_tokens, 0)
        else:
            resp = {"id": f"chatcmpl-scripted-{digest}", "object": "chat.completion",
                    "model": body.get("model", "scripted"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": usage.input_tokens,
                              "completion_tokens": usage.output_tokens}}
        return 200, resp, usage


@dataclass
class OpenAICompatibleUpstream:
    """OpenAI-compatible forwarding. For Azure OpenAI pass ``azure_deployment_base`` (…/
    openai/deployments/<name>) and ``api_version``; the key is sent as ``api-key``."""

    base_url: str
    api_key: str
    name: str = "openai-compatible"
    azure_deployment_base: str | None = None
    api_version: str | None = None
    transport: httpx.AsyncBaseTransport | None = None

    async def complete(self, api: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], Usage]:
        path = "/chat/completions" if api == "openai.chat" else "/embeddings"
        if self.azure_deployment_base:
            url = f"{self.azure_deployment_base}{path}?api-version={self.api_version}"
            headers = {"api-key": self.api_key}
        else:
            url = f"{self.base_url.rstrip('/')}{path}"
            headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(transport=self.transport, timeout=120) as client:
            r = await client.post(url, json=body, headers=headers)
        data = r.json() if r.content else {}
        u = data.get("usage") or {}
        return r.status_code, data, Usage(u.get("prompt_tokens"), u.get("completion_tokens"))


@dataclass
class AnthropicUpstream:
    api_key: str
    base_url: str = "https://api.anthropic.com"
    version: str = "2023-06-01"
    name: str = "anthropic"
    transport: httpx.AsyncBaseTransport | None = None

    async def complete(self, api: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], Usage]:
        headers = {"x-api-key": self.api_key, "anthropic-version": self.version}
        async with httpx.AsyncClient(transport=self.transport, timeout=120) as client:
            r = await client.post(f"{self.base_url.rstrip('/')}/v1/messages", json=body,
                                  headers=headers)
        data = r.json() if r.content else {}
        u = data.get("usage") or {}
        return r.status_code, data, Usage(u.get("input_tokens"), u.get("output_tokens"))
