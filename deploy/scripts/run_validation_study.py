#!/usr/bin/env python3
"""Operator entry point for the coherence-vs-outcome validation study (prompt 44).

Wraps :func:`coherence_engine.server.fund.services.validation_study.run_study`
with a deploy-friendly CLI. Same arguments as ``coherence-engine
validation-study run``; the script exists so ops can invoke the harness
without depending on an editable install of the CLI package.

Exit codes:
  0  — report written.
  2  — INSUFFICIENT_SAMPLE or another validation-study error.
  1  — unexpected exception (re-raised after printing).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_PARENT = _REPO_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))

from coherence_engine.server.fund.services import (
    validation_study as vs,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_validation_study",
        description=(
            "Deterministically run the coherence-vs-outcome regression "
            "study and emit the canonical JSON report."
        ),
    )
    parser.add_argument("--output", required=True, type=str)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--manifest", type=str, default=None)
    parser.add_argument("--outcomes", type=str, default=None)
    parser.add_argument("--scores", type=str, default=None)
    parser.add_argument("--preregistration", type=str, default=None)
    parser.add_argument("--bootstrap-iters", type=int, default=10000)
    args = parser.parse_args()

    config = vs.StudyConfig(
        preregistration_path=(
            Path(args.preregistration)
            if args.preregistration
            else vs.DEFAULT_PREREGISTRATION_PATH
        ),
        corpus_manifest_path=Path(args.manifest) if args.manifest else None,
        outcomes_path=Path(args.outcomes) if args.outcomes else None,
        coherence_scores_path=Path(args.scores) if args.scores else None,
        output_path=Path(args.output),
        seed=int(args.seed),
        bootstrap_iters=int(args.bootstrap_iters),
    )
    try:
        report = vs.run_study(config)
    except vs.InsufficientSampleError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except vs.ValidationStudyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "wrote": str(Path(args.output).resolve()),
                "report_digest": report.report_digest(),
                "n_known_outcome": report.n_known_outcome,
                "n_total": report.n_total,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
