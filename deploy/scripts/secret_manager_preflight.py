#!/usr/bin/env python3
"""Deployment preflight for secret-manager policy and wiring."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure imports work when run as a standalone script.
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PARENT = REPO_ROOT.parent
for p in (str(REPO_PARENT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from coherence_engine.server.fund.services.secret_manager import (
    SecretManagerError,
    probe_secret_manager_reachability,
    validate_secret_manager_policy,
)


def _provider() -> str:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "disabled").strip().lower()


def _strict_policy_enabled() -> bool:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true").strip().lower() == "true"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-fast preflight for secret-manager safety posture.",
    )
    parser.add_argument(
        "--secret-ref",
        default=None,
        help="Secret reference to probe. Defaults to COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF.",
    )
    parser.add_argument(
        "--allow-disabled",
        action="store_true",
        help="Allow provider=disabled (default: fail).",
    )
    parser.add_argument(
        "--allow-non-strict",
        action="store_true",
        help="Allow COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY=false (default: fail).",
    )
    parser.add_argument(
        "--require-reachable",
        action="store_true",
        help="Require reachability probe to succeed (recommended for production).",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    args = parser.parse_args()

    payload: dict = {
        "ok": False,
        "provider": _provider(),
        "strict_policy": _strict_policy_enabled(),
    }

    try:
        validate_secret_manager_policy()
        provider = _provider()
        if provider in {"", "disabled", "none"} and not args.allow_disabled:
            raise SecretManagerError(
                "secret manager provider is disabled (set --allow-disabled to bypass for non-prod)"
            )
        if not _strict_policy_enabled() and not args.allow_non_strict:
            raise SecretManagerError(
                "strict policy is disabled (set --allow-non-strict to bypass for non-prod)"
            )

        secret_ref = args.secret_ref or os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "")
        probe = probe_secret_manager_reachability(secret_ref)
        payload["probe"] = probe

        if args.require_reachable and not probe.get("reachable", False):
            raise SecretManagerError(f"secret manager probe not reachable: {probe.get('detail', '')}")
        if os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true").lower() == "true":
            if not probe.get("reachable", False):
                raise SecretManagerError(
                    "bootstrap admin is enabled but secret manager probe is not reachable"
                )

        payload["ok"] = True
    except SecretManagerError as exc:
        payload["error"] = str(exc)
    except Exception as exc:  # pragma: no cover
        payload["error"] = f"unexpected_error: {exc}"

    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if payload.get("ok"):
            print("preflight_ok provider={provider}".format(provider=payload.get("provider")))
            probe = payload.get("probe", {})
            print(
                "probe_status={status} reachable={reachable} detail={detail}".format(
                    status=probe.get("status", "unknown"),
                    reachable=probe.get("reachable", False),
                    detail=probe.get("detail", ""),
                )
            )
        else:
            print("preflight_failed provider={provider}".format(provider=payload.get("provider")))
            print("reason={reason}".format(reason=payload.get("error", "unknown")))

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

