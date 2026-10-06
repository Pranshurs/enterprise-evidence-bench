"""Run the recording gateway as a service (used inside the isolation runner)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from eeb.harness.gateway import Gateway
from eeb.harness.upstreams import ScriptedUpstream


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--upstream", choices=["scripted"], default="scripted")
    args = ap.parse_args()
    secret = os.environ["EEB_GATEWAY_CONTROL_SECRET"]
    gw = Gateway(Path(args.log), ScriptedUpstream(), control_secret=secret)
    uvicorn.run(gw.app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
