#!/usr/bin/env python3
"""Run the offline historical backtest pipeline (local-only, deterministic).

Delegates to ``coherence_engine.server.fund.services.backtest.run_backtest``.
Also available as:

  python -m coherence_engine backtest-run ...

This wrapper performs no network I/O, never reads the live portfolio
state (the snapshot is always loaded from ``--portfolio-snapshot``), and
never mutates ``data/governed/*``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a loose script: package root is the repo folder named
# ``coherence_engine``; its parent directory must be on ``sys.path`` (same
# layout as ``pytest`` / editable installs / merge_governed_historical_outcomes.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_PARENT = _REPO_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))

from coherence_engine.server.fund.services.backtest import (  # noqa: E402
    BacktestConfig,
    BacktestError,
    run_backtest,
)
from coherence_engine.server.fund.services.decision_policy import (  # noqa: E402
    DECISION_POLICY_VERSION,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True, help="Governed-format JSONL/JSON dataset")
    p.add_argument(
        "--policy-version",
        type=str,
        default=DECISION_POLICY_VERSION,
        help="Decision policy version pin (defaults to the running version)",
    )
    p.add_argument(
        "--portfolio-snapshot",
        type=Path,
        default=None,
        help="JSON file describing a fixed PortfolioSnapshot",
    )
    p.add_argument("--output", type=Path, default=None, help="Where to write the JSON report")
    p.add_argument("--seed", type=int, default=0, help="Reserved for reproducibility")
    p.add_argument("--requested-check-usd", type=float, default=50_000.0)
    p.add_argument("--domain-default", type=str, default="market_economics")
    args = p.parse_args()

    config = BacktestConfig(
        dataset_path=args.dataset,
        decision_policy_version=args.policy_version,
        portfolio_snapshot_path=args.portfolio_snapshot,
        output_path=args.output,
        seed=int(args.seed),
        requested_check_usd=float(args.requested_check_usd),
        domain_default=str(args.domain_default),
    )

    try:
        report = run_backtest(config)
    except BacktestError as exc:
        print(f"run_backtest: {exc}", file=sys.stderr)
        return 2

    sys.stdout.buffer.write(report.to_canonical_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
