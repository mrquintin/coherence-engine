#!/usr/bin/env python3
"""Validate JSON/JSONL historical outcome exports before merge into governed dataset (local-only).

Also available as:
  python -m coherence_engine uncertainty-profile validate-historical-export ...
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

from coherence_engine.server.fund.services.governed_historical_dataset import (
    validate_historical_outcomes_export,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True, help="JSON array or JSONL export path")
    p.add_argument(
        "--require-standard-layer-keys",
        action="store_true",
        help="Require all five standard layer_scores keys on each normalized row",
    )
    p.add_argument(
        "--json-summary-out",
        type=Path,
        default=None,
        help="Optional path to write validation summary JSON",
    )
    args = p.parse_args()

    try:
        rep = validate_historical_outcomes_export(
            args.input,
            require_standard_layer_keys=args.require_standard_layer_keys,
        )
    except (OSError, ValueError, FileNotFoundError) as e:
        print(f"validate_historical_outcomes_export: {e}", file=sys.stderr)
        return 1

    summary = {
        "ok": rep.ok,
        "source_path": rep.source_path,
        "rows_total": rep.rows_total,
        "valid_rows": rep.valid_rows,
        "invalid_rows": rep.invalid_rows,
        "require_standard_layer_keys": rep.require_standard_layer_keys,
        "errors": list(rep.errors),
    }
    if args.json_summary_out:
        args.json_summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_summary_out.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if rep.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
