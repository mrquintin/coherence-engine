#!/usr/bin/env python3
"""Regenerate the TypeScript SDK consumed by the founder portal.

Wraps `openapi-typescript-codegen` (lazy-installed via `npx --yes`) so the
toolchain does not have to be a permanent dependency. Reads
``docs/specs/openapi_v1.yaml`` and writes the generated client into
``apps/founder_portal/src/sdk/``.

Usage:
    python scripts/generate_ts_sdk.py
    python scripts/generate_ts_sdk.py --check  # exit 1 if regeneration would change files
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_SPEC = REPO_ROOT / "docs" / "specs" / "openapi_v1.yaml"
SDK_OUTPUT = REPO_ROOT / "apps" / "founder_portal" / "src" / "sdk"
GENERATOR_PACKAGE = "openapi-typescript-codegen@0.29.0"


def _run_generator(output_dir: Path) -> None:
    if shutil.which("npx") is None:
        sys.stderr.write(
            "npx is required to run the TypeScript SDK generator. "
            "Install Node.js 20+ (and the bundled npm) and try again.\n"
        )
        sys.exit(2)

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npx",
        "--yes",
        GENERATOR_PACKAGE,
        "--input",
        str(OPENAPI_SPEC),
        "--output",
        str(output_dir),
        "--client",
        "fetch",
        "--useOptions",
        "--useUnionTypes",
    ]
    subprocess.run(cmd, check=True)


def _check_only(tmp_dir: Path) -> int:
    if not SDK_OUTPUT.exists():
        sys.stderr.write(f"SDK directory missing: {SDK_OUTPUT}\n")
        return 1
    diff = subprocess.run(
        ["diff", "-r", str(SDK_OUTPUT), str(tmp_dir)],
        capture_output=True,
        text=True,
    )
    if diff.returncode != 0:
        sys.stderr.write("Generated SDK is out of date. Run scripts/generate_ts_sdk.py.\n")
        sys.stderr.write(diff.stdout)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify SDK is up to date.")
    args = parser.parse_args()

    if not OPENAPI_SPEC.exists():
        sys.stderr.write(f"OpenAPI spec not found: {OPENAPI_SPEC}\n")
        return 1

    if args.check:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "sdk"
            _run_generator(tmp_path)
            return _check_only(tmp_path)

    if SDK_OUTPUT.exists():
        for child in SDK_OUTPUT.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    _run_generator(SDK_OUTPUT)
    print(f"Wrote TypeScript SDK to {SDK_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
