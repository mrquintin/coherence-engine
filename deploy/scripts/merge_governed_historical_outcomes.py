#!/usr/bin/env python3
"""Merge governed uncertainty historical outcome JSONL files (local-only, deterministic).

Delegates to ``coherence_engine.server.fund.services.governed_historical_dataset``.
Also available as:

  python -m coherence_engine uncertainty-profile merge-historical-dataset ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a loose script: package root is the repo folder named ``coherence_engine``;
# its parent directory must be on ``sys.path`` (same layout as ``pytest`` / editable installs).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_PARENT = _REPO_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))

from coherence_engine.server.fund.services.governed_historical_dataset import (
    merge_governed_historical_datasets,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True, help="Base governed .jsonl")
    p.add_argument(
        "--incoming",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Incoming JSON or JSONL (repeatable)",
    )
    p.add_argument("--output", type=Path, required=True, help="Merged JSONL output path")
    p.add_argument("--manifest-out", type=Path, required=True, help="Manifest JSON output path")
    p.add_argument("--provenance-out", type=Path, default=None, help="Optional merge provenance JSON")
    p.add_argument(
        "--dataset-name",
        type=str,
        default=None,
        help="Manifest dataset field (default: basename of --output)",
    )
    p.add_argument(
        "--prefer",
        choices=("incoming", "base"),
        default="incoming",
        help="Duplicate-row resolution when fingerprints collide",
    )
    p.add_argument(
        "--strict-incoming",
        action="store_true",
        help="Fail on invalid incoming rows (default: skip)",
    )
    args = p.parse_args()

    try:
        result = merge_governed_historical_datasets(
            args.dataset,
            list(args.incoming),
            dataset_name=args.dataset_name or args.output.name,
            prefer=args.prefer,
            strict_incoming=args.strict_incoming,
        )
    except (OSError, ValueError, FileNotFoundError) as e:
        print(f"merge_governed_historical_outcomes: {e}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result.body)
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(
        json.dumps(result.manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.provenance_out:
        args.provenance_out.write_text(
            json.dumps(result.provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {**result.provenance, "checksum_sha256": result.manifest["checksum_sha256"]},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
