#!/usr/bin/env python3
"""Synthetic on-call alert drill: send one test alert using COHERENCE_FUND_OPS_ALERT_* config.

Exits with status 1 when routing is enabled but delivery fails or (with --verify-only) when
static verification reports blocking issues. Worker processes never use this script; they use
route_worker_ops_alert which swallows errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure imports work when run as a standalone script (same pattern as secret_manager_preflight).
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PARENT = REPO_ROOT.parent
for p in (str(REPO_PARENT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from coherence_engine.server.fund.services.alert_routing import (
    drill_route_worker_ops_alert,
    load_alert_router_config,
    verify_alert_router_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only run static config checks (no HTTP/file delivery). Exit 1 if issues are reported.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable result line to stdout.",
    )
    args = parser.parse_args()
    cfg = load_alert_router_config()
    issues = verify_alert_router_config(cfg)

    if args.verify_only:
        if issues:
            if args.json:
                print(json.dumps({"ok": False, "phase": "verify", "issues": issues}))
            else:
                for line in issues:
                    print(line, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": True, "phase": "verify", "issues": []}))
        return 0

    strict_verify = os.getenv("COHERENCE_FUND_OPS_ALERT_DRILL_STRICT_VERIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if strict_verify and issues:
        if args.json:
            print(json.dumps({"ok": False, "phase": "verify", "issues": issues}))
        else:
            for line in issues:
                print(line, file=sys.stderr)
        return 1

    result = drill_route_worker_ops_alert()
    payload = {"ok": result.ok, "detail": result.detail, "channel": result.channel}
    if args.json:
        print(json.dumps(payload))
    elif not result.ok:
        print(result.detail, file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
