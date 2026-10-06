"""Recording model gateway (spec §9.1; ADR-0005).

Data plane (reachable by the SUT): model endpoints under ``/r/{request_id}/``. Control plane
(``/_control/*``): bearer secret held only by the harness, used to open and close request
windows. The gateway records each call in the hash-chained log *before* forwarding it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from eeb.harness.recorder import Recorder
from eeb.harness.upstreams import Upstream

HEADER = "x-bench-request-id"
ROUTES = {
    ("v1", "chat", "completions"): "openai.chat",
    ("v1", "embeddings"): "openai.embeddings",
    ("v1", "messages"): "anthropic.messages",
}


@dataclass
class Window:
    request_id: str
    kind: str  # "request" | "ingest"
    principal_id: str | None
    question: str | None
    open: bool = True


def new_request_id() -> str:
    return secrets.token_hex(16)


class Gateway:
    def __init__(self, log_path: Path, upstream: Upstream, control_secret: str | None = None,
                 strict_sequential: bool = True) -> None:
        self.recorder = Recorder(log_path)
        self.upstream = upstream
        self.control_secret = control_secret or secrets.token_hex(32)
        self.strict_sequential = strict_sequential
        self.windows: dict[str, Window] = {}
        self.app = self._build_app()

    # ------------------------------------------------------------------ control (harness)
    def open_window(self, request_id: str, kind: str = "request", principal_id: str | None = None,
                    question: str | None = None) -> None:
        if request_id in self.windows:
            raise ValueError("request ids are single-use")
        if self.strict_sequential and any(w.open for w in self.windows.values()):
            raise RuntimeError("strict sequential mode: another window is open")
        self.windows[request_id] = Window(request_id, kind, principal_id, question)
        self.recorder.append({"type": "window_open", "request_id": request_id, "kind": kind,
                              "principal_id": principal_id})

    def close_window(self, request_id: str) -> None:
        w = self.windows[request_id]
        w.open = False
        self.recorder.append({"type": "window_close", "request_id": request_id})

    # ------------------------------------------------------------------ data plane
    def _anomaly(self, reason: str, path: str, path_id: str | None, header_id: str | None,
                 body: bytes, status: int) -> JSONResponse:
        self.recorder.append({
            "type": "anomaly", "reason": reason, "path": path, "path_request_id": path_id,
            "header_request_id": header_id, "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_text": body.decode("utf-8", "replace"), "reached_model": False,
        })
        return JSONResponse({"error": {"type": "bench_gateway", "message": reason}},
                            status_code=status)

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.post("/_control/{action}")
        async def control(action: str, request: Request) -> JSONResponse:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {self.control_secret}"
            if not hmac.compare_digest(auth.encode(), expected.encode()):
                return self._anomaly("control_unauthorized", f"/_control/{action}", None, None,
                                     await request.body(), 403)
            data = await request.json()
            if action == "open":
                self.open_window(data["request_id"], data.get("kind", "request"),
                                 data.get("principal_id"), data.get("question"))
            elif action == "close":
                self.close_window(data["request_id"])
            elif action == "get":
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(data["url"])
                return JSONResponse({"status": r.status_code, "body": r.json()
                                     if r.content else None})
            elif action == "ask":
                # Harness-to-SUT relay: in isolated runs the SUT is reachable only on the
                # internal network, which the gateway shares; the harness is outside it.
                async with httpx.AsyncClient(timeout=data.get("timeout", 300)) as client:
                    r = await client.post(data["url"], json=data["payload"])
                return JSONResponse({"status": r.status_code, "body": r.json()
                                     if r.content else None})
            else:
                return JSONResponse({"error": "unknown action"}, status_code=404)
            return JSONResponse({"ok": True})

        @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def data_plane(full_path: str, request: Request) -> JSONResponse:
            body = await request.body()
            header_id = request.headers.get(HEADER)
            parts = tuple(p for p in full_path.split("/") if p)
            if len(parts) < 2 or parts[0] != "r":
                return self._anomaly("unknown_path", "/" + full_path, None, header_id, body, 404)
            path_id, rest = parts[1], parts[2:]
            api = ROUTES.get(rest)
            if request.method != "POST" or api is None:
                return self._anomaly("unknown_endpoint", "/" + full_path, path_id, header_id,
                                     body, 404)
            w = self.windows.get(path_id)
            if w is None:
                return self._anomaly("unknown_request_id", "/" + full_path, path_id, header_id,
                                     body, 403)
            if not w.open:
                return self._anomaly("window_closed", "/" + full_path, path_id, header_id,
                                     body, 403)
            if header_id is None:
                return self._anomaly("missing_request_header", "/" + full_path, path_id, None,
                                     body, 400)
            if not hmac.compare_digest(header_id.encode(), path_id.encode()):
                return self._anomaly("header_path_mismatch", "/" + full_path, path_id,
                                     header_id, body, 400)
            try:
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise ValueError
            except ValueError:
                return self._anomaly("non_json_body", "/" + full_path, path_id, header_id,
                                     body, 400)
            if parsed.get("stream"):
                return self._anomaly("streaming_unsupported_v1", "/" + full_path, path_id,
                                     header_id, body, 400)
            sha = hashlib.sha256(body).hexdigest()
            self.recorder.append({
                "type": "model_call", "request_id": path_id, "window_kind": w.kind,
                "principal_id": w.principal_id, "api": api, "body_sha256": sha,
                "body_text": body.decode("utf-8"), "upstream": self.upstream.name,
            })
            t0 = time.monotonic()
            status, resp, usage = await self.upstream.complete(api, parsed)
            self.recorder.append({
                "type": "model_result", "request_id": path_id, "body_sha256": sha,
                "status": status, "reached_model": True,
                "response_text": json.dumps(resp, sort_keys=True, ensure_ascii=False),
                "usage": {"input_tokens": usage.input_tokens,
                          "output_tokens": usage.output_tokens},
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 3),
            })
            return JSONResponse(resp, status_code=status)

        return app
