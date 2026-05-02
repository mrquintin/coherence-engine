"""Validate the SLO recording + alert rule files (prompt 63).

Two layers of validation:

1. Structural / contract checks (always run): YAML parses cleanly, the
   expected groups exist, every recording-rule name is one we declare
   in ``docs/ops/slos.md``, and every burn-rate alert has the matching
   ``severity`` / ``burn_rate`` / window labels the runbook depends on.

2. ``promtool check rules`` (lazy): if ``promtool`` is on PATH, run
   it; otherwise skip with a clear warning. We never auto-install
   promtool — operators bring their own Prometheus toolchain.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest


_DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy" / "prometheus"
_RECORDING_RULES = _DEPLOY_DIR / "recording_rules.yaml"
_ALERT_RULES = _DEPLOY_DIR / "alert_rules.yaml"


# Contract: SLOs declared in docs/ops/slos.md.
_DECLARED_SLOS = {"slo_avail", "slo_latency", "slo_scoring", "slo_calibration"}

# Contract: burn-rate windows in the multi-window / multi-burn-rate scheme.
_BURN_RATE_LONG_WINDOWS = {"1h", "6h", "24h", "72h"}
_BURN_RATE_SHORT_WINDOWS = {"5m", "30m", "2h", "6h"}


def _yaml_load(path: Path) -> Dict[str, Any]:
    """Parse YAML using PyYAML if available, else a strict pure-Python
    fallback. The rule files are simple enough that we don't need the
    full YAML 1.2 grammar.
    """
    pytest.importorskip("yaml", reason="PyYAML required to parse rule files")
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ----------------------------------------------------------------------
# Structural checks
# ----------------------------------------------------------------------


def test_rule_files_exist() -> None:
    assert _RECORDING_RULES.exists(), f"missing {_RECORDING_RULES}"
    assert _ALERT_RULES.exists(), f"missing {_ALERT_RULES}"


def test_recording_rules_parse_and_have_expected_group() -> None:
    payload = _yaml_load(_RECORDING_RULES)
    assert isinstance(payload, dict) and "groups" in payload
    group_names = {g.get("name") for g in payload["groups"]}
    assert "coherence_fund_slo_recording_rules" in group_names, (
        f"recording_rules.yaml must define the "
        f"'coherence_fund_slo_recording_rules' group; got {group_names}"
    )


def test_recording_rules_cover_each_slo() -> None:
    payload = _yaml_load(_RECORDING_RULES)
    record_names: List[str] = []
    for group in payload["groups"]:
        for rule in group.get("rules", []):
            if "record" in rule:
                record_names.append(str(rule["record"]))

    # Every declared SLO must have at least one recording rule.
    for slo in _DECLARED_SLOS:
        assert any(slo in name for name in record_names), (
            f"recording_rules.yaml: no rule mentions SLO {slo!r}; "
            f"records present: {record_names}"
        )

    # Each ratio-based SLO must materialize all four burn-rate long
    # windows so the alert joins resolve.
    for slo in {"slo_avail", "slo_latency", "slo_scoring"}:
        for window in _BURN_RATE_LONG_WINDOWS:
            needle = f"coherence_fund:{slo}:error_ratio:{window}"
            assert needle in record_names, (
                f"recording_rules.yaml: missing {needle!r}"
            )


def test_alert_rules_parse_and_have_expected_groups() -> None:
    payload = _yaml_load(_ALERT_RULES)
    assert isinstance(payload, dict) and "groups" in payload
    group_names = {g.get("name") for g in payload["groups"]}
    assert "coherence_fund_slo_burn_rate_alerts" in group_names
    assert "coherence_fund_slo_calibration_alerts" in group_names


def _iter_alerts(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group in payload["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                out.append(rule)
    return out


def test_alert_rules_have_burn_rate_pairs() -> None:
    payload = _yaml_load(_ALERT_RULES)
    alerts = _iter_alerts(payload)

    # Each ratio-based SLO must have exactly four burn-rate alerts.
    by_slo: Dict[str, List[Dict[str, Any]]] = {}
    for alert in alerts:
        labels = alert.get("labels", {}) or {}
        slo = labels.get("slo")
        if slo:
            by_slo.setdefault(slo, []).append(alert)

    for slo in {"SLO_avail", "SLO_latency", "SLO_scoring_success"}:
        assert slo in by_slo, f"alert_rules.yaml: no alerts for {slo!r}"
        assert len(by_slo[slo]) == 4, (
            f"alert_rules.yaml: {slo!r} should have 4 burn-rate alerts, "
            f"got {len(by_slo[slo])}"
        )

        # Burn rates must be exactly {14.4, 6, 3, 1}.
        burn_rates = {str(a["labels"]["burn_rate"]) for a in by_slo[slo]}
        assert burn_rates == {"14.4", "6", "3", "1"}, (
            f"alert_rules.yaml: {slo!r} burn rates {burn_rates!r} != "
            f"{{'14.4', '6', '3', '1'}}"
        )


def test_alert_rules_severity_routing_is_correct() -> None:
    """Fast / medium burns page (critical); slow / very-slow burns
    ticket (warning). Per the prompt, never page on slow burn.
    """
    payload = _yaml_load(_ALERT_RULES)
    alerts = _iter_alerts(payload)

    for alert in alerts:
        labels = alert.get("labels", {}) or {}
        burn_rate = labels.get("burn_rate")
        severity = labels.get("severity")
        if burn_rate is None:
            # The calibration freshness alert has no burn_rate label.
            continue

        if str(burn_rate) in {"14.4", "6"}:
            assert severity == "critical", (
                f"{alert['alert']}: burn_rate={burn_rate} must page "
                f"(severity=critical), got {severity!r}"
            )
        elif str(burn_rate) in {"3", "1"}:
            assert severity == "warning", (
                f"{alert['alert']}: burn_rate={burn_rate} must ticket "
                f"(severity=warning) — never page on slow burn; "
                f"got {severity!r}"
            )
        else:  # pragma: no cover — guard against accidental new burn rates
            pytest.fail(
                f"{alert['alert']}: unexpected burn_rate {burn_rate!r}"
            )


def test_alert_rules_use_burn_rate_window_pairs() -> None:
    payload = _yaml_load(_ALERT_RULES)
    alerts = _iter_alerts(payload)

    for alert in alerts:
        labels = alert.get("labels", {}) or {}
        if "burn_rate" not in labels:
            continue
        long_w = labels.get("long_window")
        short_w = labels.get("short_window")
        assert long_w in _BURN_RATE_LONG_WINDOWS, (
            f"{alert['alert']}: long_window {long_w!r} not in "
            f"{_BURN_RATE_LONG_WINDOWS}"
        )
        assert short_w in _BURN_RATE_SHORT_WINDOWS, (
            f"{alert['alert']}: short_window {short_w!r} not in "
            f"{_BURN_RATE_SHORT_WINDOWS}"
        )

        # The alert expression must reference both windows on the
        # error_ratio recording rule — defends against a regression
        # where someone removes the short-window join.
        expr = alert.get("expr", "")
        assert f":error_ratio:{long_w}" in expr, (
            f"{alert['alert']}: expr does not reference long window "
            f"recording rule for {long_w}"
        )
        assert f":error_ratio:{short_w}" in expr, (
            f"{alert['alert']}: expr does not reference short window "
            f"recording rule for {short_w}"
        )


def test_alert_rules_never_use_raw_threshold_for_ratio_slos() -> None:
    """Per the prompt: never alert on raw thresholds. All ratio-based
    SLOs must alert through the recording-rule layer.
    """
    payload = _yaml_load(_ALERT_RULES)
    alerts = _iter_alerts(payload)

    for alert in alerts:
        labels = alert.get("labels", {}) or {}
        if labels.get("slo") in {"SLO_avail", "SLO_latency", "SLO_scoring_success"}:
            expr = alert.get("expr", "")
            assert "coherence_fund:slo_" in expr, (
                f"{alert['alert']}: must use recording rules, not raw "
                f"metric thresholds; expr={expr!r}"
            )


def test_calibration_freshness_alert_is_ticket_only() -> None:
    payload = _yaml_load(_ALERT_RULES)
    alerts = _iter_alerts(payload)
    matched = [
        a for a in alerts
        if (a.get("labels") or {}).get("slo") == "SLO_calibration_freshness"
    ]
    assert len(matched) == 1, (
        f"SLO_calibration_freshness should have exactly one alert; "
        f"got {len(matched)}"
    )
    assert matched[0]["labels"]["severity"] == "warning", (
        "SLO_calibration_freshness must ticket, never page"
    )


# ----------------------------------------------------------------------
# Lazy promtool integration
# ----------------------------------------------------------------------


def _promtool_available() -> bool:
    return shutil.which("promtool") is not None


@pytest.mark.parametrize(
    "rule_file",
    [_RECORDING_RULES, _ALERT_RULES],
    ids=lambda p: p.name,
)
def test_promtool_check_rules(rule_file: Path) -> None:
    if not _promtool_available():
        pytest.skip(
            "promtool not on PATH; install Prometheus toolchain "
            "(brew install prometheus or download from prometheus.io) "
            "to enable this validation"
        )

    result = subprocess.run(
        ["promtool", "check", "rules", str(rule_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"promtool check rules {rule_file.name} failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
