#!/usr/bin/env python3
"""Export governed historical outcome rows from scored event payloads + outcomes annotations.

Joins CoherenceScored event payloads with an operator-provided outcomes annotation
file to produce rows matching
``deploy/ops/uncertainty-historical-outcomes-export.example.json``.

Also available as:
  python -m coherence_engine uncertainty-profile export-historical-outcomes ...

Exit codes:
  0 — export succeeded (may include skipped rows; see summary)
  1 — fatal error (missing file, bad format)
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

from coherence_engine.server.fund.services.calibration_export import (  # noqa: E402
    build_export_rows,
    export_rows_to_json,
    export_rows_to_jsonl,
    load_outcomes_annotations,
)
from coherence_engine.server.fund.services.uncertainty_calibration import (  # noqa: E402
    load_historical_records,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="export_historical_outcomes",
        description="Export governed historical outcome rows from scored events + outcomes annotations.",
    )
    parser.add_argument(
        "--scored-events",
        type=str,
        required=True,
        help="Path to JSON array or JSONL of CoherenceScored event payloads",
    )
    parser.add_argument(
        "--outcomes",
        type=str,
        required=True,
        help="Path to outcomes annotation file (JSON object, array, or JSONL)",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Write governed export rows here (JSON or JSONL)",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["json", "jsonl"],
        default=None,
        help="Output format (default: inferred from --output extension)",
    )
    parser.add_argument(
        "--require-standard-layer-keys",
        action="store_true",
        help="Require all five standard layer_scores keys",
    )
    parser.add_argument(
        "--summary-out",
        type=str,
        default=None,
        help="Optional path to write export summary JSON",
    )
    args = parser.parse_args()

    events_path = Path(args.scored_events)
    if not events_path.is_file():
        print(f"Error: scored-events file not found: {events_path}", file=sys.stderr)
        return 1
    outcomes_path = Path(args.outcomes)
    if not outcomes_path.is_file():
        print(f"Error: outcomes file not found: {outcomes_path}", file=sys.stderr)
        return 1

    try:
        raw_events = load_historical_records(str(events_path))
        outcomes = load_outcomes_annotations(outcomes_path)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    result = build_export_rows(
        raw_events,
        outcomes,
        require_all_layer_keys=args.require_standard_layer_keys,
    )

    out_path = Path(args.output)
    fmt = args.format
    if fmt is None:
        fmt = "jsonl" if out_path.suffix.lower() in (".jsonl",) else "json"
    body = export_rows_to_jsonl(result.rows) if fmt == "jsonl" else export_rows_to_json(result.rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")

    summary = {
        "ok": result.skipped_invalid == 0,
        "output": str(out_path.resolve()),
        "format": fmt,
        "rows_exported": len(result.rows),
        "skipped_no_outcome": result.skipped_no_outcome,
        "skipped_invalid": result.skipped_invalid,
        "warnings": list(result.warnings),
    }
    if args.summary_out:
        Path(args.summary_out).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
