"""Minimal stdlib-only SUT for isolation tests. Behaviour is chosen by SUT_MODE:
  compliant   one model call through the gateway carrying CONTEXT
  bypass      tries to reach an external provider host directly (TCP connect only; no content
              is ever sent), then makes a clean gateway call
"""

import json
import os
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

GATEWAY = os.environ["GATEWAY_URL"]
MODE = os.environ.get("SUT_MODE", "compliant")


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ask = json.loads(self.rfile.read(int(self.headers["content-length"])))
        rid, context = ask["request_id"], ask.get("context", "")
        body = {"model": "m", "messages": [{"role": "user",
                                            "content": f"{ask['question']}\n{context}"}]}
        notes = []
        if MODE == "bypass":
            try:
                with socket.create_connection(("1.1.1.1", 443), timeout=3):
                    notes.append("bypass:egress_open")
            except OSError as e:
                notes.append(f"bypass:egress_blocked:{type(e).__name__}")
            body = {"model": "m", "messages": [{"role": "user", "content": ask["question"]}]}
        reply = post(f"{GATEWAY}/r/{rid}/v1/chat/completions", body,
                     {"x-bench-request-id": rid})
        out = {"outcome": "ANSWER",
               "answer_text": reply["choices"][0]["message"]["content"], "notes": notes}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


HTTPServer(("0.0.0.0", 8000), H).serve_forever()
