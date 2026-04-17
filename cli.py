"""Command-line interface for the Coherence Engine."""

import argparse
import sys
import os
import json


def main():
    parser = argparse.ArgumentParser(
        prog="coherence-engine",
        description="Measure the internal logical coherence of any text (0-1 scale).",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── analyze ───────────────────────────────────────────────
    analyze_p = subparsers.add_parser("analyze", help="Score a text or file")
    analyze_p.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Text string or path to a .txt file. Reads stdin if omitted.",
    )
    analyze_p.add_argument(
        "--format", "-f",
        choices=["text", "json", "markdown"],
        default="text",
        help="Output format (default: text)",
    )
    analyze_p.add_argument("--verbose", "-v", action="store_true", help="Show extra detail")
    analyze_p.add_argument(
        "--weights",
        type=str,
        default=None,
        help="Custom layer weights as comma-separated floats: "
             "contradiction,argumentation,embedding,compression,structural",
    )
    analyze_p.set_defaults(delegate_large=True)
    analyze_p.add_argument(
        "--no-delegate-large",
        dest="delegate_large",
        action="store_false",
        help="Disable automatic large-prompt delegation during analyze",
    )
    analyze_p.add_argument(
        "--force-parallel",
        type=int,
        default=None,
        help="Force splitting across N parallel agents (1-4) during analyze",
    )
    analyze_p.add_argument(
        "--agent-list-file",
        type=str,
        default=None,
        help="Path to JSON list of custom agent profiles for analyze delegation",
    )
    analyze_p.add_argument(
        "--agent-list",
        type=str,
        default=None,
        help="Comma-separated agent names to enable during analyze delegation",
    )
    analyze_p.add_argument(
        "--auto-threshold-words",
        type=int,
        default=1000,
        help="Word threshold for automatic analyze delegation (default: 1000)",
    )
    analyze_p.add_argument(
        "--auto-threshold-chars",
        type=int,
        default=7000,
        help="Character threshold for automatic analyze delegation (default: 7000)",
    )

    # ── compare ───────────────────────────────────────────────
    compare_p = subparsers.add_parser("compare", help="Score and compare against a domain")
    compare_p.add_argument("input", help="Text string or path to a file")
    compare_p.add_argument(
        "--domain", "-d",
        type=str,
        default=None,
        help="Domain key to compare against (auto-detected if omitted)",
    )
    compare_p.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # ── delegate ──────────────────────────────────────────────
    delegate_p = subparsers.add_parser(
        "delegate",
        help="Split large prompts across parallel subagents (up to 4)",
    )
    delegate_p.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Prompt string or path to a .txt file. Reads stdin if omitted.",
    )
    delegate_p.add_argument(
        "--format", "-f",
        choices=["text", "json", "markdown"],
        default="text",
        help="Output format for each delegate run (default: text)",
    )
    delegate_p.set_defaults(auto_delegate=True)
    delegate_p.add_argument(
        "--no-auto-delegate",
        dest="auto_delegate",
        action="store_false",
        help="Disable automatic delegation for large prompts",
    )
    delegate_p.add_argument(
        "--force-parallel",
        type=int,
        default=None,
        help="Force splitting across N parallel agents (1-4)",
    )
    delegate_p.add_argument(
        "--agent-list-file",
        type=str,
        default=None,
        help="Path to JSON list of custom agent profiles",
    )
    delegate_p.add_argument(
        "--agent-list",
        type=str,
        default=None,
        help="Comma-separated agent names to enable from the available list",
    )
    delegate_p.add_argument(
        "--auto-threshold-words",
        type=int,
        default=1000,
        help="Word threshold for automatic delegation (default: 1000)",
    )
    delegate_p.add_argument(
        "--auto-threshold-chars",
        type=int,
        default=7000,
        help="Character threshold for automatic delegation (default: 7000)",
    )
    delegate_p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show extra detail",
    )

    # ── serve ─────────────────────────────────────────────────
    serve_p = subparsers.add_parser("serve", help="Start the HTTP API server")
    serve_p.add_argument("--port", "-p", type=int, default=8000, help="Port (default: 8000)")
    serve_p.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")

    # ── serve-fund ────────────────────────────────────────────
    serve_fund_p = subparsers.add_parser("serve-fund", help="Start the starter pre-seed fund API")
    serve_fund_p.add_argument("--port", "-p", type=int, default=8010, help="Port (default: 8010)")
    serve_fund_p.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")

    # ── dispatch-outbox ───────────────────────────────────────
    dispatch_p = subparsers.add_parser("dispatch-outbox", help="Dispatch fund outbox events to Kafka/SQS/Redis")
    dispatch_p.add_argument("--backend", choices=["kafka", "sqs", "redis"], required=True)
    dispatch_p.add_argument("--run-mode", choices=["once", "loop"], default="once")
    dispatch_p.add_argument("--batch-size", type=int, default=100)
    dispatch_p.add_argument("--poll-seconds", type=float, default=2.0)
    dispatch_p.add_argument("--topic-prefix", type=str, default="coherence.fund")
    dispatch_p.add_argument("--max-attempts", type=int, default=5)
    dispatch_p.add_argument("--retry-base-seconds", type=int, default=2)
    dispatch_p.add_argument("--kafka-bootstrap-servers", type=str, default="")
    dispatch_p.add_argument("--sqs-queue-url", type=str, default="")
    dispatch_p.add_argument("--sqs-region", type=str, default="us-east-1")
    dispatch_p.add_argument("--redis-url", type=str, default="")

    # ── process-scoring-jobs ──────────────────────────────────
    score_jobs_p = subparsers.add_parser("process-scoring-jobs", help="Process queued fund scoring jobs")
    score_jobs_p.add_argument("--run-mode", choices=["once", "loop"], default="once")
    score_jobs_p.add_argument("--max-jobs", type=int, default=100)
    score_jobs_p.add_argument("--poll-seconds", type=float, default=2.0)
    score_jobs_p.add_argument("--worker-id", type=str, default=None)
    score_jobs_p.add_argument("--lease-seconds", type=int, default=120)
    score_jobs_p.add_argument("--retry-base-seconds", type=int, default=5)

    # ── replay-outbox ─────────────────────────────────────────
    replay_outbox_p = subparsers.add_parser("replay-outbox", help="Replay failed outbox events (dead-letter)")
    replay_outbox_p.add_argument("--event-id", action="append", default=[], help="Specific failed outbox event id")
    replay_outbox_p.add_argument("--all-failed", action="store_true", help="Replay all failed outbox events")
    replay_outbox_p.add_argument("--limit", type=int, default=100)
    replay_outbox_p.add_argument("--reset-attempts", action="store_true")

    # ── replay-scoring-jobs ───────────────────────────────────
    replay_jobs_p = subparsers.add_parser("replay-scoring-jobs", help="Replay failed scoring jobs (dead-letter)")
    replay_jobs_p.add_argument("--job-id", action="append", default=[], help="Specific failed scoring job id")
    replay_jobs_p.add_argument("--all-failed", action="store_true", help="Replay all failed scoring jobs")
    replay_jobs_p.add_argument("--limit", type=int, default=100)
    replay_jobs_p.add_argument("--reset-attempts", action="store_true")

    # ── create-fund-api-key ───────────────────────────────────
    create_key_p = subparsers.add_parser("create-fund-api-key", help="Create a DB-backed fund API key")
    create_key_p.add_argument("--label", required=True)
    create_key_p.add_argument("--role", choices=["viewer", "analyst", "admin"], required=True)
    create_key_p.add_argument("--expires-in-days", type=int, default=None)
    create_key_p.add_argument("--created-by", type=str, default="cli")
    create_key_p.add_argument("--secret-ref", type=str, default=None, help="Secret manager ref to write token")

    # ── revoke-fund-api-key ───────────────────────────────────
    revoke_key_p = subparsers.add_parser("revoke-fund-api-key", help="Revoke a DB-backed fund API key")
    revoke_key_p.add_argument("--key-id", required=True)

    # ── rotate-fund-api-key ───────────────────────────────────
    rotate_key_p = subparsers.add_parser("rotate-fund-api-key", help="Rotate a DB-backed fund API key")
    rotate_key_p.add_argument("--key-id", required=True)
    rotate_key_p.add_argument("--expires-in-days", type=int, default=None)
    rotate_key_p.add_argument("--secret-ref", type=str, default=None, help="Secret manager ref to write rotated token")

    # ── layers ────────────────────────────────────────────────
    subparsers.add_parser("layers", help="List available layers and their status")

    # ── version ───────────────────────────────────────────────
    subparsers.add_parser("version", help="Print version and dependency info")

    # ── calibrate-uncertainty ─────────────────────────────────
    calib_p = subparsers.add_parser(
        "calibrate-uncertainty",
        help="Calibrate superiority uncertainty constants from historical JSON/JSONL",
    )
    calib_p.add_argument(
        "input_path",
        help="Path to JSON array or JSONL of historical scoring records",
    )
    calib_p.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Write calibration JSON to this path (still prints to stdout)",
    )
    calib_p.add_argument(
        "--target-coverage",
        type=float,
        default=0.95,
        help="Nominal coverage target for the objective (default: 0.95)",
    )
    calib_p.add_argument(
        "--width-penalty",
        type=float,
        default=1.0,
        help="Weight on mean interval width in the objective (default: 1.0)",
    )

    # ── uncertainty-profile ───────────────────────────────────
    up_p = subparsers.add_parser(
        "uncertainty-profile",
        help="Promote or rollback uncertainty calibration profiles (shadow / canary / prod)",
    )
    up_sub = up_p.add_subparsers(
        dest="uncertainty_profile_command",
        required=True,
        help="Subcommands",
    )
    up_prom = up_sub.add_parser(
        "promote",
        help="Set stage active profile from a JSON file; previous active goes to rollback stack",
    )
    up_prom.add_argument(
        "--registry",
        type=str,
        required=True,
        help="Path to the local JSON registry file",
    )
    up_prom.add_argument(
        "--stage",
        type=str,
        required=True,
        choices=["shadow", "canary", "prod"],
        help="Deployment stage",
    )
    up_prom.add_argument(
        "--profile",
        type=str,
        required=True,
        help="Path to calibration JSON (e.g. calibrate-uncertainty output)",
    )
    up_prom.add_argument(
        "--reason",
        type=str,
        default="",
        help="Optional note stored in registry history",
    )
    up_prom.add_argument(
        "--governance-audit-log",
        type=str,
        default=None,
        help="Append signed JSONL audit record after successful promote",
    )
    up_prom.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        help="Governance gate: minimum calibration metrics.coverage (requires profile JSON metrics)",
    )
    up_prom.add_argument(
        "--max-mean-width",
        type=float,
        default=None,
        help="Governance gate: maximum calibration metrics.mean_width",
    )
    up_prom.add_argument(
        "--min-record-count",
        type=int,
        default=None,
        help="Governance gate: minimum n_records_used (or equivalent)",
    )
    up_prom.add_argument(
        "--baseline-profile",
        type=str,
        default=None,
        help="Optional baseline calibration JSON for delta gates",
    )
    up_prom.add_argument(
        "--max-coverage-drop",
        type=float,
        default=None,
        help="Max allowed drop vs baseline metrics.coverage (requires --baseline-profile)",
    )
    up_prom.add_argument(
        "--max-mean-width-increase",
        type=float,
        default=None,
        help="Max allowed increase vs baseline metrics.mean_width (requires --baseline-profile)",
    )
    up_prom.add_argument(
        "--force",
        action="store_true",
        help="Bypass failed governance gates and still promote (recorded in audit as forced)",
    )
    up_prom.add_argument(
        "--governance-policy",
        type=str,
        default=None,
        help="Path to uncertainty_governance_policy.json; thresholds for --stage (CLI gate flags override)",
    )
    up_rb = up_sub.add_parser(
        "rollback",
        help="Restore prior active profile for a stage (LIFO rollback stack)",
    )
    up_rb.add_argument("--registry", type=str, required=True)
    up_rb.add_argument(
        "--stage",
        type=str,
        required=True,
        choices=["shadow", "canary", "prod"],
    )
    up_rb.add_argument(
        "--governance-audit-log",
        type=str,
        default=None,
        help="Append signed JSONL audit record after successful rollback",
    )
    up_rb.add_argument(
        "--health-metrics-json",
        type=str,
        default=None,
        help="Optional health metrics file for rollback trigger context in audit record",
    )
    up_rb.add_argument(
        "--policy-min-coverage",
        type=float,
        default=None,
        help="With --health-metrics-json: flag rollback trigger if coverage below this",
    )
    up_rb.add_argument(
        "--policy-max-mean-width",
        type=float,
        default=None,
        help="With --health-metrics-json: flag rollback trigger if mean_width above this",
    )
    up_rb.add_argument(
        "--policy-min-record-count",
        type=int,
        default=None,
        help="With --health-metrics-json: flag rollback trigger if record count below this",
    )
    up_rb.add_argument(
        "--governance-policy",
        type=str,
        default=None,
        help="Merge stages.<stage>.rollback_triggers from this JSON with explicit --policy-* flags",
    )
    up_show = up_sub.add_parser(
        "show",
        help="Print registry JSON (optionally one stage)",
    )
    up_show.add_argument("--registry", type=str, required=True)
    up_show.add_argument(
        "--stage",
        type=str,
        default=None,
        choices=["shadow", "canary", "prod"],
        help="If set, print only this stage's block",
    )
    up_verify = up_sub.add_parser(
        "verify-dataset",
        help="Verify a governed dataset file matches its manifest checksum",
    )
    up_verify.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Path to governed historical outcomes (.jsonl or file named in manifest)",
    )
    up_verify.add_argument(
        "--manifest",
        type=str,
        required=True,
        help="Path to manifest JSON (checksum_sha256)",
    )
    up_merge = up_sub.add_parser(
        "merge-historical-dataset",
        help="Merge governed historical outcome JSON/JSONL files and refresh manifest (local-only)",
    )
    up_merge.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Base governed dataset (.jsonl)",
    )
    up_merge.add_argument(
        "--incoming",
        dest="merge_incoming",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional JSON or JSONL file to merge (repeatable)",
    )
    up_merge.add_argument(
        "--output",
        type=str,
        required=True,
        help="Write merged JSONL here",
    )
    up_merge.add_argument(
        "--manifest-out",
        type=str,
        required=True,
        help="Write manifest JSON (checksum_sha256) here",
    )
    up_merge.add_argument(
        "--provenance-out",
        type=str,
        default=None,
        help="Optional JSON path recording merge stats and source paths",
    )
    up_merge.add_argument(
        "--dataset-name",
        type=str,
        default=None,
        help="Manifest dataset field (default: basename of --output)",
    )
    up_merge.add_argument(
        "--prefer",
        type=str,
        choices=["incoming", "base"],
        default="incoming",
        help="When duplicate logical rows collide, keep incoming (default) or base",
    )
    up_merge.add_argument(
        "--strict-incoming",
        action="store_true",
        help="Fail if any incoming row cannot be normalized (default: skip invalid incoming rows)",
    )
    up_val_exp = up_sub.add_parser(
        "validate-historical-export",
        help="Validate JSON/JSONL rows before merge into governed historical outcomes (local-only)",
    )
    up_val_exp.add_argument(
        "--input",
        type=str,
        required=True,
        help="Path to JSON array or JSONL export",
    )
    up_val_exp.add_argument(
        "--require-standard-layer-keys",
        action="store_true",
        help="Require contradiction/argumentation/embedding/compression/structural in layer_scores",
    )
    up_val_exp.add_argument(
        "--json-summary-out",
        type=str,
        default=None,
        help="Optional path to write validation summary JSON",
    )
    up_export = up_sub.add_parser(
        "export-historical-outcomes",
        help="Extract scored event payloads + outcomes annotations into governed export rows (local-only)",
    )
    up_export.add_argument(
        "--scored-events",
        type=str,
        required=True,
        help="Path to JSON array of CoherenceScored event payloads (or JSONL, or outbox dump)",
    )
    up_export.add_argument(
        "--outcomes",
        type=str,
        required=True,
        help="Path to outcomes annotation file (JSON object, array, or JSONL with application_id + outcome_superiority)",
    )
    up_export.add_argument(
        "--output",
        type=str,
        required=True,
        help="Write governed export rows here (JSON or JSONL based on extension)",
    )
    up_export.add_argument(
        "--format",
        type=str,
        choices=["json", "jsonl"],
        default=None,
        help="Output format (default: inferred from --output extension)",
    )
    up_export.add_argument(
        "--require-standard-layer-keys",
        action="store_true",
        help="Require all five standard layer_scores keys in each exported row",
    )
    up_export.add_argument(
        "--summary-out",
        type=str,
        default=None,
        help="Optional path to write export summary JSON",
    )
    up_eval = up_sub.add_parser(
        "evaluate-gates",
        help="Evaluate objective quality gates on a calibration JSON (no registry change)",
    )
    up_eval.add_argument(
        "--profile",
        type=str,
        required=True,
        help="Path to calibration output JSON",
    )
    up_eval.add_argument("--baseline-profile", type=str, default=None)
    up_eval.add_argument("--min-coverage", type=float, default=None)
    up_eval.add_argument("--max-mean-width", type=float, default=None)
    up_eval.add_argument("--min-record-count", type=int, default=None)
    up_eval.add_argument("--max-coverage-drop", type=float, default=None)
    up_eval.add_argument("--max-mean-width-increase", type=float, default=None)
    up_eval.add_argument(
        "--governance-policy",
        type=str,
        default=None,
        help="Load promotion_gates for --policy-stage (CLI gate flags override policy)",
    )
    up_eval.add_argument(
        "--policy-stage",
        type=str,
        default=None,
        choices=["shadow", "canary", "prod"],
        help="Stage key in policy file (required with --governance-policy for evaluate-gates)",
    )
    up_upol = up_sub.add_parser(
        "rollback-policy-eval",
        help="Evaluate automated rollback triggers from a JSON metrics file (local, no I/O besides read)",
    )
    up_upol.add_argument(
        "--metrics-json",
        type=str,
        required=True,
        help="Path to health/calibration metrics JSON",
    )
    up_upol.add_argument("--min-coverage", type=float, default=None)
    up_upol.add_argument("--max-mean-width", type=float, default=None)
    up_upol.add_argument("--min-record-count", type=int, default=None)
    up_upol.add_argument(
        "--governance-policy",
        type=str,
        default=None,
        help="Load stages.<policy-stage>.rollback_triggers (merged with explicit rollback flags)",
    )
    up_upol.add_argument(
        "--policy-stage",
        type=str,
        default=None,
        choices=["shadow", "canary", "prod"],
        help="Required with --governance-policy for rollback-policy-eval",
    )

    # ── prompt-registry ───────────────────────────────────────
    pr_p = subparsers.add_parser(
        "prompt-registry",
        help="Inspect and verify the versioned LLM prompt registry (SHA-pinned)",
    )
    pr_sub = pr_p.add_subparsers(
        dest="prompt_registry_command",
        required=True,
        help="Subcommands",
    )
    pr_list = pr_sub.add_parser("list", help="List all prompts in the registry")
    pr_list.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to a non-default prompt registry JSON file",
    )
    pr_list.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human-readable table",
    )
    pr_verify = pr_sub.add_parser(
        "verify",
        help="Recompute body SHA-256 values; exit 2 if any mismatch or missing body",
    )
    pr_verify.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to a non-default prompt registry JSON file",
    )
    pr_verify.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON verification report",
    )
    pr_digest = pr_sub.add_parser(
        "digest",
        help="Print the stable registry digest used as prompt_registry_digest pin",
    )
    pr_digest.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to a non-default prompt registry JSON file",
    )

    # ── portfolio-state ───────────────────────────────────────
    pstate_p = subparsers.add_parser(
        "portfolio-state",
        help="Inspect or record portfolio state (NAV, liquidity reserve, regime, positions)",
    )
    pstate_sub = pstate_p.add_subparsers(
        dest="portfolio_state_command",
        required=True,
        help="Subcommands",
    )
    pstate_show = pstate_sub.add_parser(
        "show",
        help="Print the latest portfolio state and per-domain active concentration as JSON",
    )
    pstate_show.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent (0 for compact)",
    )
    pstate_set_reserve = pstate_sub.add_parser(
        "set-reserve",
        help="Append a new portfolio-state row with the liquidity reserve updated to <usd>",
    )
    pstate_set_reserve.add_argument(
        "--usd",
        type=float,
        required=True,
        help="Liquidity reserve in USD (must be >= 0)",
    )
    pstate_set_reserve.add_argument(
        "--note",
        type=str,
        default=None,
        help="Optional note attached to the new snapshot",
    )

    # ── backtest-run ──────────────────────────────────────────
    backtest_p = subparsers.add_parser(
        "backtest-run",
        help=(
            "Replay the governed historical-outcomes dataset through the "
            "current scorer + decision policy with a fixed portfolio "
            "snapshot, and emit a deterministic JSON report."
        ),
    )
    backtest_p.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Path to a governed-format JSONL (or JSON array) dataset",
    )
    backtest_p.add_argument(
        "--policy-version",
        type=str,
        required=True,
        help=(
            "Decision policy version pin to assert against the running "
            "DECISION_POLICY_VERSION (mismatch exits with 2)"
        ),
    )
    backtest_p.add_argument(
        "--portfolio-snapshot",
        type=str,
        default=None,
        help=(
            "Path to a JSON file describing a fixed PortfolioSnapshot "
            "(omit for an all-zero default snapshot)"
        ),
    )
    backtest_p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Where to write the deterministic JSON report (omit for stdout-only)",
    )
    backtest_p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Reserved for future use; recorded in the report for reproducibility",
    )
    backtest_p.add_argument(
        "--requested-check-usd",
        type=float,
        default=50_000.0,
        help="Per-row requested check size used for portfolio-gate evaluation",
    )
    backtest_p.add_argument(
        "--domain-default",
        type=str,
        default="market_economics",
        help="Domain key applied to rows that omit one",
    )

    # ── red-team-run ─────────────────────────────────────────
    red_team_p = subparsers.add_parser(
        "red-team-run",
        help=(
            "Replay the curated adversarial fixture corpus through the "
            "scoring + decision pipeline and emit a deterministic JSON "
            "report with per-case verdicts, false-pass / false-reject / "
            "false-review counts, and a confusion matrix."
        ),
    )
    red_team_p.add_argument(
        "--fixtures-dir",
        type=str,
        required=True,
        help="Directory containing *.json adversarial fixtures",
    )
    red_team_p.add_argument(
        "--labels",
        type=str,
        required=True,
        help="Path to labels.json mapping fixture filename -> expected_verdict",
    )
    red_team_p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Where to write the deterministic JSON report (omit for stdout-only)",
    )
    red_team_p.add_argument(
        "--policy-version",
        type=str,
        default=None,
        help=(
            "Optional decision-policy version pin; defaults to the "
            "currently running DECISION_POLICY_VERSION. Mismatch exits 2."
        ),
    )

    # ── application (set-mode) ───────────────────────────────
    application_p = subparsers.add_parser(
        "application",
        help="Application lifecycle maintenance verbs (set-mode, ...)",
    )
    application_sub = application_p.add_subparsers(
        dest="application_command",
        required=True,
        help="Subcommands",
    )
    app_set_mode = application_sub.add_parser(
        "set-mode",
        help=(
            "Set an application's scoring_mode to enforce|shadow. "
            "Refuses enforce->shadow after a decision has been issued "
            "unless --force is provided."
        ),
    )
    app_set_mode.add_argument(
        "--application-id",
        type=str,
        required=True,
        help="Application id (e.g. app_abc123)",
    )
    app_set_mode.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["enforce", "shadow"],
        help="New scoring mode",
    )
    app_set_mode.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Allow enforce->shadow transition even if a decision already "
            "exists (use with care — retroactively suppresses downstream "
            "side effects tied to the decision)"
        ),
    )

    # ── workflow run / workflow resume ───────────────────────
    workflow_p = subparsers.add_parser(
        "workflow",
        help=(
            "Run or resume the fund application workflow orchestrator "
            "(prompt 15). Each stage writes a checkpoint row so "
            "retries resume at the failing stage."
        ),
    )
    workflow_sub = workflow_p.add_subparsers(
        dest="workflow_command",
        required=True,
        help="Subcommands",
    )
    workflow_run_p = workflow_sub.add_parser(
        "run",
        help=(
            "Execute the full pipeline "
            "(intake -> transcript_quality -> compile -> ontology -> "
            "domain_mix -> score -> decide -> artifact -> notify) "
            "for an application and print a JSON summary."
        ),
    )
    workflow_run_p.add_argument(
        "--application-id",
        type=str,
        required=True,
        help="Application id (e.g. app_abc123)",
    )
    workflow_resume_p = workflow_sub.add_parser(
        "resume",
        help=(
            "Resume the most recent non-succeeded workflow run for an "
            "application, picking up at the first non-succeeded step. "
            "Refuses if any already-succeeded step's input_digest has "
            "drifted unless --force is supplied."
        ),
    )
    workflow_resume_p.add_argument(
        "--application-id",
        type=str,
        required=True,
        help="Application id (e.g. app_abc123)",
    )
    workflow_resume_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Bypass the input_digest drift check on succeeded steps. "
            "Use only when upstream mutation is intentional."
        ),
    )

    # ── gui ───────────────────────────────────────────────────
    subparsers.add_parser("gui", help="Launch the graphical interface")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "version":
        _cmd_version()
    elif args.command == "uncertainty-profile":
        _cmd_uncertainty_profile(args)
    elif args.command == "calibrate-uncertainty":
        _cmd_calibrate_uncertainty(args)
    elif args.command == "layers":
        _cmd_layers()
    elif args.command == "analyze":
        _cmd_analyze(args)
    elif args.command == "compare":
        _cmd_compare(args)
    elif args.command == "delegate":
        _cmd_delegate(args)
    elif args.command == "serve":
        _cmd_serve(args)
    elif args.command == "serve-fund":
        _cmd_serve_fund(args)
    elif args.command == "dispatch-outbox":
        _cmd_dispatch_outbox(args)
    elif args.command == "process-scoring-jobs":
        _cmd_process_scoring_jobs(args)
    elif args.command == "replay-outbox":
        _cmd_replay_outbox(args)
    elif args.command == "replay-scoring-jobs":
        _cmd_replay_scoring_jobs(args)
    elif args.command == "create-fund-api-key":
        _cmd_create_fund_api_key(args)
    elif args.command == "revoke-fund-api-key":
        _cmd_revoke_fund_api_key(args)
    elif args.command == "rotate-fund-api-key":
        _cmd_rotate_fund_api_key(args)
    elif args.command == "prompt-registry":
        _cmd_prompt_registry(args)
    elif args.command == "portfolio-state":
        _cmd_portfolio_state(args)
    elif args.command == "backtest-run":
        _cmd_backtest_run(args)
    elif args.command == "red-team-run":
        _cmd_red_team_run(args)
    elif args.command == "application":
        _cmd_application(args)
    elif args.command == "workflow":
        _cmd_workflow(args)
    elif args.command == "gui":
        _cmd_gui()


def _read_input(input_arg):
    """Read text from a file path, inline string, or stdin."""
    if input_arg and os.path.isfile(input_arg):
        with open(input_arg, "r", encoding="utf-8") as f:
            return f.read()
    elif input_arg:
        return input_arg
    elif not sys.stdin.isatty():
        return sys.stdin.read()
    else:
        print("Error: provide text, a file path, or pipe via stdin.", file=sys.stderr)
        sys.exit(1)


def _parse_weights(weights_str):
    """Parse comma-separated weight string into config dict."""
    try:
        w = [float(x) for x in weights_str.split(",")]
        if len(w) != 5:
            raise ValueError
        total = sum(w)
        if abs(total - 1.0) > 0.01:
            print(
                f"Error: --weights must sum to 1.0, got {total:.3f}",
                file=sys.stderr,
            )
            sys.exit(1)
        return {
            "weight_contradiction": w[0],
            "weight_argumentation": w[1],
            "weight_embedding": w[2],
            "weight_compression": w[3],
            "weight_structural": w[4],
        }
    except (ValueError, IndexError):
        print(
            "Error: --weights must be 5 comma-separated floats summing to 1.0",
            file=sys.stderr,
        )
        sys.exit(1)


def _promotion_gate_thresholds_from_cli_args(args):
    from coherence_engine.server.fund.services.uncertainty_governance import GateThresholds

    return GateThresholds(
        min_coverage=getattr(args, "min_coverage", None),
        max_mean_width=getattr(args, "max_mean_width", None),
        min_record_count=getattr(args, "min_record_count", None),
        max_coverage_drop_vs_baseline=getattr(args, "max_coverage_drop", None),
        max_mean_width_increase_vs_baseline=getattr(args, "max_mean_width_increase", None),
    )


def _resolve_promotion_gate_thresholds(args, *, stage: str):
    """
    Merge file policy for ``stage`` with CLI gate flags (CLI wins when set).

    ``stage`` is only used when ``--governance-policy`` is set; otherwise ignored.
    Returns (merged thresholds, needs_baseline, gates_on, policy_doc | None).
    """
    from coherence_engine.server.fund.services.uncertainty_governance import (
        GateThresholds,
        gate_thresholds_any_set,
        load_uncertainty_governance_policy,
        merge_gate_thresholds,
    )

    doc = None
    base = GateThresholds()
    path = getattr(args, "governance_policy", None)
    if path:
        doc = load_uncertainty_governance_policy(path)
        base = doc.promotion_gate_thresholds(stage)
    merged = merge_gate_thresholds(base, _promotion_gate_thresholds_from_cli_args(args))
    needs_baseline = (
        merged.max_coverage_drop_vs_baseline is not None
        or merged.max_mean_width_increase_vs_baseline is not None
    )
    gates_on = gate_thresholds_any_set(merged)
    return merged, needs_baseline, gates_on, doc


def _cmd_uncertainty_profile(args):
    from coherence_engine.server.fund.services.uncertainty_profile_registry import (
        RegistryError,
        load_registry,
        promote,
        read_profile_json,
        rollback,
        verify_manifest_checksum,
    )
    from coherence_engine.server.fund.services.uncertainty_governance import (
        GateEvaluation,
        GovernanceError,
        RollbackPolicy,
        append_audit_jsonl,
        build_promotion_audit_record,
        build_rollback_audit_record,
        evaluate_quality_gates,
        evaluate_rollback_trigger,
        extract_calibration_metrics,
        load_metrics_json,
        load_uncertainty_governance_policy,
        merge_rollback_policy,
        rollback_policy_any_set,
        sha256_file,
    )

    cmd = args.uncertainty_profile_command
    try:
        if cmd == "promote":
            thresholds, needs_baseline, gates_on, policy_doc = _resolve_promotion_gate_thresholds(
                args, stage=args.stage
            )
            if needs_baseline and not args.baseline_profile:
                print(
                    "Error: --max-coverage-drop and --max-mean-width-increase require --baseline-profile",
                    file=sys.stderr,
                )
                sys.exit(1)
            profile_obj = read_profile_json(args.profile)
            baseline_obj = None
            if args.baseline_profile:
                baseline_obj = read_profile_json(args.baseline_profile)
            if gates_on:
                ge = evaluate_quality_gates(
                    profile_obj,
                    thresholds,
                    baseline_calibration=baseline_obj,
                )
                if not ge.approved and not getattr(args, "force", False):
                    print(
                        json.dumps(
                            {
                                "approved": False,
                                "failures": list(ge.failures),
                                "metrics": ge.metrics,
                                "baseline_metrics": ge.baseline_metrics,
                            },
                            indent=2,
                        ),
                        file=sys.stderr,
                    )
                    print("Error: governance gates rejected candidate profile", file=sys.stderr)
                    sys.exit(1)
            else:
                ge = None

            promote(
                args.registry,
                args.stage,
                args.profile,
                reason=args.reason or "",
            )
            print(f"promoted stage={args.stage} registry={args.registry}")

            if args.governance_audit_log:
                if gates_on:
                    audit_ge = ge
                    forced = bool(not ge.approved and args.force)
                else:
                    audit_ge = GateEvaluation(
                        approved=True,
                        metrics=extract_calibration_metrics(profile_obj),
                        failures=(),
                        baseline_metrics=None,
                    )
                    forced = False
                rec = build_promotion_audit_record(
                    operation="promote",
                    stage=args.stage,
                    registry_path=args.registry,
                    profile_path=args.profile,
                    profile_sha256=sha256_file(args.profile),
                    gate_evaluation=audit_ge,
                    forced=forced,
                    reason=args.reason or "",
                    governance_policy_path=policy_doc.source_path if policy_doc else None,
                    governance_policy_schema_version=policy_doc.schema_version if policy_doc else None,
                )
                append_audit_jsonl(args.governance_audit_log, rec)
                print(f"governance_audit_appended path={args.governance_audit_log}")
        elif cmd == "rollback":
            policy_decision = None
            if args.health_metrics_json and not args.governance_audit_log:
                print(
                    "Error: --health-metrics-json requires --governance-audit-log "
                    "(policy evaluation is recorded in the audit trail)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if args.health_metrics_json:
                hm = load_metrics_json(args.health_metrics_json)
                base_rp = RollbackPolicy()
                if args.governance_policy:
                    pdoc = load_uncertainty_governance_policy(args.governance_policy)
                    br = pdoc.rollback_triggers(args.stage)
                    if br is not None:
                        base_rp = br
                cli_rp = RollbackPolicy(
                    min_coverage=args.policy_min_coverage,
                    max_mean_width=args.policy_max_mean_width,
                    min_record_count=args.policy_min_record_count,
                )
                pol = merge_rollback_policy(base_rp, cli_rp)
                if not rollback_policy_any_set(pol):
                    print(
                        "Error: with --health-metrics-json, supply at least one rollback threshold "
                        "via --governance-policy and/or --policy-min-coverage, "
                        "--policy-max-mean-width, --policy-min-record-count",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                policy_decision = evaluate_rollback_trigger(hm, pol)

            rollback(args.registry, args.stage)
            print(f"rolled_back stage={args.stage} registry={args.registry}")

            if args.governance_audit_log:
                rec = build_rollback_audit_record(
                    stage=args.stage,
                    registry_path=args.registry,
                    reason="",
                    policy_decision=policy_decision,
                )
                append_audit_jsonl(args.governance_audit_log, rec)
                print(f"governance_audit_appended path={args.governance_audit_log}")
        elif cmd == "show":
            reg = load_registry(args.registry)
            if args.stage:
                print(json.dumps(reg["stages"][args.stage], indent=2, sort_keys=True))
            else:
                print(json.dumps(reg, indent=2, sort_keys=True))
        elif cmd == "verify-dataset":
            digest = verify_manifest_checksum(args.dataset, args.manifest)
            print(f"checksum_ok sha256={digest}")
        elif cmd == "merge-historical-dataset":
            from pathlib import Path

            from coherence_engine.server.fund.services.governed_historical_dataset import (
                merge_governed_historical_datasets,
            )

            out_path = Path(args.output)
            man_path = Path(args.manifest_out)
            base = Path(args.dataset)
            incoming = [Path(p) for p in (args.merge_incoming or [])]
            ds_name = args.dataset_name or out_path.name
            result = merge_governed_historical_datasets(
                base,
                incoming,
                dataset_name=ds_name,
                prefer=args.prefer,
                strict_incoming=args.strict_incoming,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(result.body)
            man_path.parent.mkdir(parents=True, exist_ok=True)
            man_path.write_text(
                json.dumps(result.manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if args.provenance_out:
                Path(args.provenance_out).write_text(
                    json.dumps(result.provenance, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            summary = {
                **result.provenance,
                "checksum_sha256": result.manifest["checksum_sha256"],
                "output": str(out_path.resolve()),
                "manifest_out": str(man_path.resolve()),
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
        elif cmd == "validate-historical-export":
            from pathlib import Path

            from coherence_engine.server.fund.services.governed_historical_dataset import (
                validate_historical_outcomes_export,
            )

            inp = Path(args.input)
            rep = validate_historical_outcomes_export(
                inp,
                require_standard_layer_keys=args.require_standard_layer_keys,
            )
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
                Path(args.json_summary_out).write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            print(json.dumps(summary, indent=2, sort_keys=True))
            if not rep.ok:
                sys.exit(2)
        elif cmd == "export-historical-outcomes":
            from pathlib import Path

            from coherence_engine.server.fund.services.calibration_export import (
                build_export_rows,
                export_rows_to_json,
                export_rows_to_jsonl,
                load_outcomes_annotations,
            )
            from coherence_engine.server.fund.services.uncertainty_calibration import (
                load_historical_records,
            )

            events_path = Path(args.scored_events)
            if not events_path.is_file():
                print(f"Error: scored-events file not found: {events_path}", file=sys.stderr)
                sys.exit(1)
            outcomes_path = Path(args.outcomes)
            if not outcomes_path.is_file():
                print(f"Error: outcomes file not found: {outcomes_path}", file=sys.stderr)
                sys.exit(1)

            raw_events = load_historical_records(str(events_path))
            outcomes = load_outcomes_annotations(outcomes_path)

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
        elif cmd == "evaluate-gates":
            if args.governance_policy and not args.policy_stage:
                print(
                    "Error: evaluate-gates requires --policy-stage when using --governance-policy",
                    file=sys.stderr,
                )
                sys.exit(1)
            policy_stage = args.policy_stage or "shadow"
            thresholds, needs_baseline, _, _ = _resolve_promotion_gate_thresholds(
                args, stage=policy_stage
            )
            if needs_baseline and not args.baseline_profile:
                print(
                    "Error: --max-coverage-drop and --max-mean-width-increase require --baseline-profile",
                    file=sys.stderr,
                )
                sys.exit(1)
            profile_obj = read_profile_json(args.profile)
            baseline_obj = None
            if args.baseline_profile:
                baseline_obj = read_profile_json(args.baseline_profile)
            ge = evaluate_quality_gates(
                profile_obj,
                thresholds,
                baseline_calibration=baseline_obj,
            )
            print(
                json.dumps(
                    {
                        "approved": ge.approved,
                        "failures": list(ge.failures),
                        "metrics": ge.metrics,
                        "baseline_metrics": ge.baseline_metrics,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif cmd == "rollback-policy-eval":
            if args.governance_policy and not args.policy_stage:
                print(
                    "Error: rollback-policy-eval requires --policy-stage when using --governance-policy",
                    file=sys.stderr,
                )
                sys.exit(1)
            base_rp = RollbackPolicy()
            if args.governance_policy:
                pdoc = load_uncertainty_governance_policy(args.governance_policy)
                br = pdoc.rollback_triggers(args.policy_stage)
                if br is not None:
                    base_rp = br
            cli_rp = RollbackPolicy(
                min_coverage=args.min_coverage,
                max_mean_width=args.max_mean_width,
                min_record_count=args.min_record_count,
            )
            pol = merge_rollback_policy(base_rp, cli_rp)
            if not rollback_policy_any_set(pol):
                print(
                    "Error: set at least one rollback threshold via --governance-policy "
                    "and/or --min-coverage, --max-mean-width, --min-record-count",
                    file=sys.stderr,
                )
                sys.exit(1)
            hm = load_metrics_json(args.metrics_json)
            decision = evaluate_rollback_trigger(hm, pol)
            print(
                json.dumps(
                    {
                        "should_rollback": decision.should_rollback,
                        "reasons": list(decision.reasons),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Error: unknown uncertainty-profile command: {cmd}", file=sys.stderr)
            sys.exit(1)
    except RegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except GovernanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_calibrate_uncertainty(args):
    from coherence_engine.server.fund.services.uncertainty_calibration import (
        run_calibration_pipeline,
    )

    if not os.path.isfile(args.input_path):
        print(f"Error: file not found: {args.input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = run_calibration_pipeline(
            args.input_path,
            target_coverage=args.target_coverage,
            width_penalty=args.width_penalty,
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    out = json.dumps(payload, indent=2)
    print(out)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(out)
                fh.write("\n")
        except OSError as exc:
            print(f"Error: could not write --output: {exc}", file=sys.stderr)
            sys.exit(1)


def _cmd_version():
    from coherence_engine import __version__
    print(f"Coherence Engine v{__version__}")
    print()

    deps = [
        ("sentence-transformers", "SBERT embeddings (Layer 3)"),
        ("transformers", "NLI contradiction detection (Layer 1)"),
        ("torch", "GPU acceleration"),
        ("networkx", "Graph analysis (optional)"),
        ("numpy", "Numeric computation"),
        ("fastapi", "HTTP API server"),
    ]
    for pkg, desc in deps:
        try:
            mod = __import__(pkg.replace("-", "_"))
            ver = getattr(mod, "__version__", "unknown")
            status = f"v{ver}"
        except ImportError:
            status = "not installed (using fallback)"
        print(f"  {pkg:25s} {status:20s}  — {desc}")


def _cmd_layers():
    print("Available analysis layers:\n")

    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
        embed_status = "SBERT (all-mpnet-base-v2, 768-dim)"
    except ImportError:
        embed_status = "TF-IDF fallback (no sentence-transformers)"

    try:
        from transformers import AutoModelForSequenceClassification  # noqa: F401
        nli_status = "DeBERTa-v3-large NLI"
    except ImportError:
        nli_status = "Heuristic pattern matching (no transformers)"

    layers = [
        ("1. Contradiction", "0.30", nli_status),
        ("2. Argumentation", "0.20", "Dung's framework (grounded extension)"),
        ("3. Embedding", "0.20", embed_status),
        ("4. Compression", "0.15", "zlib (Kolmogorov proxy)"),
        ("5. Structural", "0.15", "Graph connectivity analysis"),
    ]

    for name, weight, detail in layers:
        print(f"  {name:22s}  weight={weight}  {detail}")


def _cmd_analyze(args):
    from coherence_engine.config import EngineConfig
    from coherence_engine.core.scorer import CoherenceScorer
    from coherence_engine.core.delegation import PromptDelegationEngine

    text = _read_input(args.input)

    if not text.strip():
        print("Error: empty input.", file=sys.stderr)
        sys.exit(1)

    selected_agents = None
    if args.agent_list:
        selected_agents = [item.strip() for item in args.agent_list.split(",") if item.strip()]

    should_consider_delegation = args.delegate_large or args.force_parallel is not None
    if should_consider_delegation:
        delegation_engine = PromptDelegationEngine(
            auto_word_threshold=args.auto_threshold_words,
            auto_char_threshold=args.auto_threshold_chars,
        )
        decision = delegation_engine.decide_delegation(
            prompt=text,
            force_parallel=args.force_parallel,
            auto_delegate=args.delegate_large,
        )
        if decision.delegated:
            try:
                delegated = delegation_engine.run(
                    prompt=text,
                    output_format=args.format,
                    force_parallel=args.force_parallel,
                    auto_delegate=args.delegate_large,
                    selected_agents=selected_agents,
                    agent_list_file=args.agent_list_file,
                    verbose=args.verbose,
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

            if args.format == "json":
                print(json.dumps(delegated, indent=2))
            else:
                _print_delegation_text_report(delegated)
            return

    config = EngineConfig(output_format=args.format, verbose=args.verbose)

    if args.weights:
        for k, v in _parse_weights(args.weights).items():
            setattr(config, k, v)

    scorer = CoherenceScorer(config)
    result = scorer.score(text)
    print(result.report(fmt=args.format))


def _cmd_compare(args):
    from coherence_engine.config import EngineConfig
    from coherence_engine.core.scorer import CoherenceScorer
    from coherence_engine.domain.comparator import DomainComparator
    import json

    text = _read_input(args.input)
    if not text.strip():
        print("Error: empty input.", file=sys.stderr)
        sys.exit(1)

    config = EngineConfig(enable_domain_comparison=True)
    scorer = CoherenceScorer(config)
    result = scorer.score(text)

    comparator = DomainComparator()
    domains = [args.domain] if args.domain else None
    comparison = comparator.compare(result, domains=domains)

    if args.format == "json":
        print(json.dumps(comparison, indent=2))
    else:
        _print_comparison_report(result, comparison)


def _cmd_delegate(args):
    from coherence_engine.core.delegation import PromptDelegationEngine

    prompt = _read_input(args.input)
    if not prompt.strip():
        print("Error: empty input.", file=sys.stderr)
        sys.exit(1)

    selected_agents = None
    if args.agent_list:
        selected_agents = [item.strip() for item in args.agent_list.split(",") if item.strip()]

    engine = PromptDelegationEngine(
        auto_word_threshold=args.auto_threshold_words,
        auto_char_threshold=args.auto_threshold_chars,
    )

    try:
        result = engine.run(
            prompt=prompt,
            output_format=args.format,
            force_parallel=args.force_parallel,
            auto_delegate=args.auto_delegate,
            selected_agents=selected_agents,
            agent_list_file=args.agent_list_file,
            verbose=args.verbose,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return

    _print_delegation_text_report(result)


def _print_delegation_text_report(result):
    print("=" * 60)
    print(" PARALLEL PROMPT DELEGATION")
    print("=" * 60)
    decision = result.get("delegation", {})
    print(f" Delegated: {decision.get('delegated')}")
    print(f" Reason: {decision.get('reason')}")
    print(f" Parallel agents used: {result.get('parallel_agents_used')}")
    print(f" Aggregate score: {result.get('aggregate_score'):.4f}")
    print("-" * 60)

    for run in result.get("runs", []):
        agent = run.get("agent", {}).get("name", "unknown")
        idx = run.get("chunk_index")
        wc = run.get("chunk_word_count")
        score = run.get("score")
        print(f" Chunk {idx}  |  agent={agent}  |  words={wc}  |  score={score:.4f}")

    print("-" * 60)
    print(" Synthesis Prompt")
    print("-" * 60)
    print(result.get("synthesis_prompt", ""))
    print("=" * 60)


def _print_comparison_report(result, comparison):
    """Print human-readable domain comparison."""
    print("=" * 50)
    print(" DOMAIN-RELATIVE COHERENCE COMPARISON")
    print("=" * 50)
    print(f" Argument Coherence: {result.composite_score:.3f}")
    print("-" * 50)

    for comp in comparison.get("comparisons", []):
        symbol = {"SUPERIOR": "+", "COMPARABLE": "=", "INFERIOR": "-"}.get(
            comp["assessment"], "?"
        )
        print(
            f"  [{symbol}] {comp['domain_name']:25s}  "
            f"domain={comp['domain_coherence']:.3f}  "
            f"diff={comp['differential']:+.3f}  "
            f"{comp['assessment']}"
        )

    tensions = comparison.get("relevant_tensions", [])
    if tensions:
        print("-" * 50)
        print(" Relevant cross-domain tensions:")
        for t in tensions:
            print(f"   {t['description']}")

    print("=" * 50)


def _cmd_serve(args):
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is required for server mode.\n"
            "Install with: pip install coherence-engine[full]",
            file=sys.stderr,
        )
        sys.exit(1)

    from coherence_engine.server.app import create_app

    app = create_app()
    print(f"Starting Coherence Engine API on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


def _cmd_serve_fund(args):
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is required for server mode.\n"
            "Install with: pip install coherence-engine[full]",
            file=sys.stderr,
        )
        sys.exit(1)

    from coherence_engine.server.fund_api import create_fund_app

    app = create_fund_app()
    print(f"Starting Starter Fund API on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


def _cmd_dispatch_outbox(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.outbox_dispatcher import OutboxDispatcher, run_loop
    from coherence_engine.server.fund.services.outbox_publishers import (
        KafkaPublisher,
        RedisPublisher,
        SQSPublisher,
    )

    if args.backend == "kafka":
        if not args.kafka_bootstrap_servers:
            print("Error: --kafka-bootstrap-servers is required for kafka backend", file=sys.stderr)
            sys.exit(1)
        publisher = KafkaPublisher(bootstrap_servers=args.kafka_bootstrap_servers)
    elif args.backend == "sqs":
        if not args.sqs_queue_url:
            print("Error: --sqs-queue-url is required for sqs backend", file=sys.stderr)
            sys.exit(1)
        publisher = SQSPublisher(queue_url=args.sqs_queue_url, region_name=args.sqs_region)
    else:
        if not args.redis_url:
            print("Error: --redis-url is required for redis backend", file=sys.stderr)
            sys.exit(1)
        publisher = RedisPublisher(redis_url=args.redis_url)

    db = SessionLocal()
    try:
        dispatcher = OutboxDispatcher(
            db=db,
            publisher=publisher,
            topic_prefix=args.topic_prefix,
            max_attempts=args.max_attempts,
            retry_base_seconds=args.retry_base_seconds,
        )
        if args.run_mode == "once":
            result = dispatcher.dispatch_once(batch_size=args.batch_size)
            print(
                f"Outbox dispatch complete: scanned={result['scanned']} "
                f"published={result['published']} failed={result['failed']}"
            )
        else:
            print(
                f"Starting outbox dispatcher loop backend={args.backend} "
                f"batch_size={args.batch_size} poll_seconds={args.poll_seconds}"
            )
            run_loop(dispatcher, poll_seconds=args.poll_seconds, batch_size=args.batch_size)
    finally:
        db.close()


def _cmd_process_scoring_jobs(args):
    from coherence_engine.server.fund.scoring_worker import process_once, run_loop

    if args.run_mode == "once":
        result = process_once(
            max_jobs=args.max_jobs,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            retry_base_seconds=args.retry_base_seconds,
        )
        print(
            f"Scoring worker run complete: processed={result['processed']} "
            f"failed={result['failed']} idle={result['idle']}"
        )
    else:
        print(
            f"Starting scoring worker loop max_jobs={args.max_jobs} "
            f"poll_seconds={args.poll_seconds}"
        )
        run_loop(
            max_jobs_per_tick=args.max_jobs,
            poll_seconds=args.poll_seconds,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            retry_base_seconds=args.retry_base_seconds,
        )


def _cmd_replay_outbox(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository

    db = SessionLocal()
    try:
        repo = OutboxRepository(db)
        event_ids = args.event_id or None
        if event_ids and args.all_failed:
            print("Error: use either --event-id or --all-failed", file=sys.stderr)
            sys.exit(1)
        if not event_ids and not args.all_failed:
            print("Error: provide --event-id or --all-failed", file=sys.stderr)
            sys.exit(1)
        replay_count = repo.replay_failed(
            event_ids=event_ids,
            limit=args.limit,
            reset_attempts=args.reset_attempts,
        )
        db.commit()
        print(f"replayed_outbox_events={replay_count}")
    finally:
        db.close()


def _cmd_replay_scoring_jobs(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository

    db = SessionLocal()
    try:
        repo = ApplicationRepository(db)
        job_ids = args.job_id or None
        if job_ids and args.all_failed:
            print("Error: use either --job-id or --all-failed", file=sys.stderr)
            sys.exit(1)
        if not job_ids and not args.all_failed:
            print("Error: provide --job-id or --all-failed", file=sys.stderr)
            sys.exit(1)
        replay_count = repo.replay_scoring_jobs(
            job_ids=job_ids,
            limit=args.limit,
            reset_attempts=args.reset_attempts,
        )
        db.commit()
        print(f"replayed_scoring_jobs={replay_count}")
    finally:
        db.close()


def _cmd_create_fund_api_key(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
    from coherence_engine.server.fund.services.api_key_service import ApiKeyService
    from coherence_engine.server.fund.services.secret_manager import SecretManagerError, get_secret_manager

    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        created = svc.create_key(
            repo=repo,
            label=args.label,
            role=args.role,
            created_by=args.created_by,
            expires_in_days=args.expires_in_days,
        )
        repo.add_audit_event(
            action="api_key_create_cli",
            success=True,
            actor=args.created_by,
            request_id="cli",
            ip="local",
            path="cli:create-fund-api-key",
            details={"key_id": created["id"], "role": created["role"], "label": created["label"]},
            api_key_id=created["id"],
        )
        if args.secret_ref:
            manager = get_secret_manager()
            if manager is None:
                print("Error: secret manager provider is not configured", file=sys.stderr)
                db.rollback()
                sys.exit(1)
            try:
                manager.put_secret(args.secret_ref, created["token"])
            except SecretManagerError as exc:
                print(f"Error: failed to write secret manager token: {exc}", file=sys.stderr)
                db.rollback()
                sys.exit(1)
            repo.add_audit_event(
                action="api_key_secret_synced_cli",
                success=True,
                actor=args.created_by,
                request_id="cli",
                ip="local",
                path="cli:create-fund-api-key",
                details={"key_id": created["id"], "secret_ref": args.secret_ref, "operation": "create"},
                api_key_id=created["id"],
            )
        db.commit()
        print(f"key_id={created['id']}")
        print(f"role={created['role']}")
        print(f"fingerprint={created['fingerprint']}")
        print(f"expires_at={created['expires_at']}")
        print(f"token={created['token']}")
    finally:
        db.close()


def _cmd_revoke_fund_api_key(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
    from coherence_engine.server.fund.services.api_key_service import ApiKeyService

    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        ok = svc.revoke_key(repo=repo, key_id=args.key_id)
        if not ok:
            print("Error: key not found", file=sys.stderr)
            sys.exit(1)
        repo.add_audit_event(
            action="api_key_revoke_cli",
            success=True,
            actor="cli",
            request_id="cli",
            ip="local",
            path="cli:revoke-fund-api-key",
            details={"key_id": args.key_id},
            api_key_id=args.key_id,
        )
        db.commit()
        print(f"revoked={args.key_id}")
    finally:
        db.close()


def _cmd_rotate_fund_api_key(args):
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
    from coherence_engine.server.fund.services.api_key_service import ApiKeyService
    from coherence_engine.server.fund.services.secret_manager import SecretManagerError, get_secret_manager

    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        rotated = svc.rotate_key(
            repo=repo,
            key_id=args.key_id,
            actor="cli",
            expires_in_days=args.expires_in_days,
        )
        if not rotated:
            print("Error: key not found", file=sys.stderr)
            sys.exit(1)
        if args.secret_ref:
            manager = get_secret_manager()
            if manager is None:
                print("Error: secret manager provider is not configured", file=sys.stderr)
                db.rollback()
                sys.exit(1)
            try:
                manager.put_secret(args.secret_ref, rotated["token"])
            except SecretManagerError as exc:
                print(f"Error: failed to write secret manager token: {exc}", file=sys.stderr)
                db.rollback()
                sys.exit(1)
            repo.add_audit_event(
                action="api_key_secret_synced_cli",
                success=True,
                actor="cli",
                request_id="cli",
                ip="local",
                path="cli:rotate-fund-api-key",
                details={"old_key_id": args.key_id, "new_key_id": rotated["id"], "secret_ref": args.secret_ref, "operation": "rotate"},
                api_key_id=rotated["id"],
            )
        db.commit()
        print(f"old_key_id={args.key_id}")
        print(f"new_key_id={rotated['id']}")
        print(f"role={rotated['role']}")
        print(f"fingerprint={rotated['fingerprint']}")
        print(f"expires_at={rotated['expires_at']}")
        print(f"token={rotated['token']}")
    finally:
        db.close()


def _cmd_prompt_registry(args):
    """Dispatch `prompt-registry` subcommands: list, verify, digest."""
    from pathlib import Path as _Path

    from coherence_engine.server.fund.services.prompt_registry import (
        PromptRegistryError,
        load_registry,
        registry_digest,
        verify_registry,
    )

    registry_path = _Path(args.registry) if getattr(args, "registry", None) else None
    try:
        registry = load_registry(registry_path)
    except PromptRegistryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    sub = getattr(args, "prompt_registry_command", None)
    as_json = bool(getattr(args, "as_json", False))

    if sub == "list":
        if as_json:
            payload = {
                "schema_version": registry.schema_version,
                "prompts": [
                    {
                        "id": e.id,
                        "version": e.version,
                        "status": e.status,
                        "body_path": e.body_path,
                        "content_sha256": e.content_sha256,
                        "owner": e.owner,
                    }
                    for e in registry.prompts
                ],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"schema_version: {registry.schema_version}")
            print(f"prompts: {len(registry.prompts)}")
            for e in registry.prompts:
                print(
                    f"  {e.id:<24} v{e.version:<8} status={e.status:<6} "
                    f"sha256={e.content_sha256[:12]}…  owner={e.owner}"
                )
        return

    if sub == "verify":
        report = verify_registry(registry)
        if as_json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            if report.ok:
                print(f"OK — {len(registry.prompts)} prompts verified")
            else:
                print("FAIL — prompt registry verification failed", file=sys.stderr)
                for m in report.mismatches:
                    print(
                        f"  mismatch: {m.prompt_id}@{m.version} {m.body_path}\n"
                        f"            expected {m.expected_sha256}\n"
                        f"            actual   {m.actual_sha256}",
                        file=sys.stderr,
                    )
                for path in report.missing:
                    print(f"  missing:  {path}", file=sys.stderr)
        if not report.ok:
            sys.exit(2)
        return

    if sub == "digest":
        print(registry_digest(registry))
        return

    print(f"Unknown prompt-registry subcommand: {sub!r}", file=sys.stderr)
    sys.exit(2)


def _cmd_portfolio_state(args):
    """Dispatch `portfolio-state` subcommands: show, set-reserve.

    The CLI opens a short-lived DB session against the configured
    ``DATABASE_URL`` and ensures the ``portfolio_state`` / ``positions``
    tables exist (via ``Base.metadata.create_all``) so that operators can
    use these verbs on a fresh environment without running migrations
    first. No trades, transfers, or ledger writes are performed.
    """
    from coherence_engine.server.fund.database import Base, engine, SessionLocal
    from coherence_engine.server.fund import models  # noqa: F401  (register mappers)
    from coherence_engine.server.fund.repositories.portfolio_repository import (
        PortfolioRepository,
    )

    Base.metadata.create_all(bind=engine)
    sub = getattr(args, "portfolio_state_command", None)
    indent = getattr(args, "indent", 2)
    indent_arg = indent if indent and indent > 0 else None

    session = SessionLocal()
    try:
        repo = PortfolioRepository(session)

        if sub == "show":
            state = repo.latest_state_as_dict()
            concentration = repo.domain_concentration_by_nav()
            totals = repo.active_positions_by_domain()
            payload = {
                "state": state,
                "active_positions_by_domain_usd": {k: round(v, 2) for k, v in totals.items()},
                "domain_concentration_by_nav": {
                    k: round(v, 6) for k, v in concentration.items()
                },
            }
            print(json.dumps(payload, indent=indent_arg, sort_keys=True, default=str))
            return

        if sub == "set-reserve":
            usd = float(args.usd)
            if usd < 0.0:
                print("Error: --usd must be >= 0", file=sys.stderr)
                sys.exit(2)
            try:
                row = repo.set_liquidity_reserve(usd, note=getattr(args, "note", None))
                session.commit()
            except Exception as exc:
                session.rollback()
                print(f"Error: set-reserve failed: {exc}", file=sys.stderr)
                sys.exit(1)
            out = {
                "id": int(row.id),
                "as_of": row.as_of.isoformat() if row.as_of else None,
                "fund_nav_usd": float(row.fund_nav_usd),
                "liquidity_reserve_usd": float(row.liquidity_reserve_usd),
                "drawdown_proxy": float(row.drawdown_proxy),
                "regime": str(row.regime),
                "note": row.note,
            }
            print(json.dumps(out, indent=indent_arg, sort_keys=True, default=str))
            return

        print(f"Unknown portfolio-state subcommand: {sub!r}", file=sys.stderr)
        sys.exit(2)
    finally:
        session.close()


def _cmd_backtest_run(args):
    """Dispatch ``backtest-run`` — replay the governed dataset through the
    current scorer + decision policy with a fixed portfolio snapshot.

    Exit codes:
      0  — backtest completed; report printed to stdout (and written to
           ``--output`` when supplied).
      2  — dataset failed validation, the policy-version pin did not
           match, or the snapshot file was unreadable. The error
           message is written to stderr and no report is produced.
    """
    from pathlib import Path as _Path

    from coherence_engine.server.fund.services.backtest import (
        BacktestConfig,
        BacktestError,
        run_backtest,
    )

    config = BacktestConfig(
        dataset_path=_Path(args.dataset),
        decision_policy_version=str(args.policy_version),
        portfolio_snapshot_path=_Path(args.portfolio_snapshot) if args.portfolio_snapshot else None,
        output_path=_Path(args.output) if args.output else None,
        seed=int(args.seed),
        requested_check_usd=float(args.requested_check_usd),
        domain_default=str(args.domain_default),
    )

    try:
        report = run_backtest(config)
    except BacktestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    sys.stdout.buffer.write(report.to_canonical_bytes())
    return 0


def _cmd_red_team_run(args):
    """Dispatch ``red-team-run`` — replay the curated adversarial corpus.

    Exit codes:
      0  — every fixture's actual verdict matched its labeled
           ``expected_verdict``.
      1  — at least one mismatch (false-pass / false-reject /
           false-review). The full report is still emitted to stdout
           (and to ``--output`` when supplied).
      2  — fixture / labels could not be loaded, or the policy-version
           pin did not match the running ``DECISION_POLICY_VERSION``.
           The error message is written to stderr and no report is
           produced.
    """
    from pathlib import Path as _Path

    from coherence_engine.server.fund.services.decision_policy import (
        DECISION_POLICY_VERSION,
    )
    from coherence_engine.server.fund.services.red_team import (
        RedTeamError,
        run_adversarial_suite,
    )

    pinned_version = (
        str(args.policy_version) if args.policy_version else DECISION_POLICY_VERSION
    )

    try:
        report = run_adversarial_suite(
            fixtures_dir=_Path(args.fixtures_dir),
            labels_path=_Path(args.labels),
            policy_version=pinned_version,
        )
    except RedTeamError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    payload = report.to_canonical_bytes()
    if args.output:
        out_path = _Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(payload)
    sys.stdout.buffer.write(payload)

    if report.mismatches > 0:
        sys.exit(1)
    return 0


def _cmd_application(args):
    """Dispatch ``application`` subcommands.

    Currently supports:

    * ``set-mode --application-id <id> --mode enforce|shadow [--force]``
      — transition an application between ``enforce`` (production) and
      ``shadow`` (side-effect-suppressed) scoring modes. Refuses
      ``enforce -> shadow`` after a decision has been issued unless
      ``--force`` is supplied (prompt 12 guardrail). Exit codes:
      ``0`` on success (including no-op when already in the requested
      mode), ``2`` on validation error (missing application, forbidden
      transition, bad mode value).
    """
    from coherence_engine.server.fund.database import Base, SessionLocal, engine
    from coherence_engine.server.fund import models  # noqa: F401  (register mappers)
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import EventPublisher

    Base.metadata.create_all(bind=engine)
    sub = getattr(args, "application_command", None)

    session = SessionLocal()
    try:
        repo = ApplicationRepository(session)
        events = EventPublisher(session, strict_events=False)
        service = ApplicationService(repo, events)

        if sub == "set-mode":
            try:
                result = service.set_scoring_mode(
                    application_id=str(args.application_id),
                    new_mode=str(args.mode),
                    force=bool(args.force),
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)
            except RuntimeError as exc:
                session.rollback()
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)
            print(json.dumps(result, sort_keys=True))
            return

        print(f"Unknown application subcommand: {sub!r}", file=sys.stderr)
        sys.exit(2)
    finally:
        session.close()


def _cmd_workflow(args):
    """Dispatch ``workflow`` subcommands (prompt 15).

    * ``workflow run --application-id <id>`` — execute the full
      pipeline end-to-end. Exit code ``0`` on success (workflow
      status ``succeeded``), ``1`` if any stage raises (workflow
      status ``failed``), ``2`` on validation errors (missing
      application id, unknown application).
    * ``workflow resume --application-id <id> [--force]`` —
      resume the most recent non-succeeded workflow run for the
      application. Exit code ``3`` if resume is refused due to an
      ``input_digest`` drift on an already-succeeded step (pass
      ``--force`` to bypass).
    """
    from coherence_engine.server.fund.database import Base, SessionLocal, engine
    from coherence_engine.server.fund import models  # noqa: F401  (register mappers)
    from coherence_engine.server.fund.services.workflow import (
        WorkflowError,
        WorkflowResumeRefused,
        run_workflow,
    )

    Base.metadata.create_all(bind=engine)
    sub = getattr(args, "workflow_command", None)
    application_id = str(getattr(args, "application_id", "") or "")
    force = bool(getattr(args, "force", False))

    session = SessionLocal()
    try:
        try:
            if sub == "run":
                run = run_workflow(session, application_id, resume=False)
            elif sub == "resume":
                run = run_workflow(
                    session, application_id, resume=True, force=force
                )
            else:
                print(f"Unknown workflow subcommand: {sub!r}", file=sys.stderr)
                sys.exit(2)
        except WorkflowResumeRefused as exc:
            session.commit()
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(3)
        except WorkflowError as exc:
            session.rollback()
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            session.commit()
            print(
                f"Error: workflow_stage_failed:{type(exc).__name__}:{exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        session.commit()
        summary = {
            "workflow_run_id": run.id,
            "application_id": run.application_id,
            "status": run.status,
            "current_step": run.current_step,
            "started_at": (
                run.started_at.isoformat() if run.started_at else None
            ),
            "finished_at": (
                run.finished_at.isoformat() if run.finished_at else None
            ),
            "error": run.error or "",
        }
        print(json.dumps(summary, sort_keys=True))
        if run.status != "succeeded":
            sys.exit(1)
        return 0
    finally:
        session.close()


def _cmd_gui():
    from coherence_engine.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
