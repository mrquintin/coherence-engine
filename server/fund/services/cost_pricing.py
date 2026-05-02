"""Cost-pricing registry loader (prompt 62).

Loads ``data/governed/cost_pricing.yaml`` once and exposes the lookup
``get_price(sku)`` used by :mod:`cost_telemetry` to compute the
``unit_cost_usd`` and ``total_usd`` columns on a ``CostEvent`` row.

Pricing MUST live in YAML, never in code -- a price change is a
governed YAML edit, not a deploy. The loader fails loud on a
schema-version mismatch or an unknown SKU so a stale-config bug never
silently flows into a wrong cost number.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional


__all__ = [
    "COST_PRICING_SCHEMA_VERSION",
    "CostPricingError",
    "PriceEntry",
    "get_price",
    "load_pricing_registry",
    "reset_pricing_cache",
]


COST_PRICING_SCHEMA_VERSION = "cost-pricing-v1"

_DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data"
    / "governed"
    / "cost_pricing.yaml"
)


class CostPricingError(RuntimeError):
    """Raised when the pricing registry is malformed or a SKU is unknown."""


@dataclass(frozen=True)
class PriceEntry:
    """A single ``sku → unit + unit_cost_usd`` row from the YAML registry."""

    sku: str
    unit: str
    unit_cost_usd: float


_CACHE_LOCK = threading.Lock()
_CACHED: Optional[Dict[str, PriceEntry]] = None
_CACHED_PATH: Optional[Path] = None


def _parse_entry(raw: Mapping[str, object]) -> PriceEntry:
    sku = str(raw.get("sku") or "").strip()
    if not sku:
        raise CostPricingError("cost_pricing_entry_missing_sku")
    unit = str(raw.get("unit") or "").strip()
    if not unit:
        raise CostPricingError(f"cost_pricing_entry_missing_unit:{sku}")
    try:
        unit_cost_usd = float(raw.get("unit_cost_usd"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CostPricingError(
            f"cost_pricing_entry_unit_cost_not_number:{sku}"
        ) from exc
    if unit_cost_usd < 0:
        raise CostPricingError(f"cost_pricing_entry_unit_cost_negative:{sku}")
    return PriceEntry(sku=sku, unit=unit, unit_cost_usd=unit_cost_usd)


def load_pricing_registry(
    path: Optional[Path | str] = None,
) -> Dict[str, PriceEntry]:
    """Load and validate the YAML pricing registry.

    The result is cached per-path on first load; pass a different
    ``path`` (or call :func:`reset_pricing_cache`) to force a re-read.
    """
    global _CACHED, _CACHED_PATH

    target = Path(path) if path is not None else _DEFAULT_PRICING_PATH

    with _CACHE_LOCK:
        if (
            _CACHED is not None
            and _CACHED_PATH is not None
            and _CACHED_PATH == target
        ):
            return dict(_CACHED)

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dep
        raise CostPricingError("pyyaml_required_for_cost_pricing") from exc

    if not target.exists():
        raise CostPricingError(f"cost_pricing_file_missing:{target}")

    with target.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, Mapping):
        raise CostPricingError("cost_pricing_must_be_mapping")

    schema = str(raw.get("schema_version") or "").strip()
    if schema != COST_PRICING_SCHEMA_VERSION:
        raise CostPricingError(
            f"cost_pricing_schema_mismatch:want={COST_PRICING_SCHEMA_VERSION}"
            f" got={schema!r}"
        )

    prices_raw = raw.get("prices") or []
    if not isinstance(prices_raw, list):
        raise CostPricingError("cost_pricing_prices_must_be_list")

    entries: Dict[str, PriceEntry] = {}
    for entry_raw in prices_raw:
        if not isinstance(entry_raw, Mapping):
            raise CostPricingError("cost_pricing_entry_must_be_mapping")
        entry = _parse_entry(entry_raw)
        if entry.sku in entries:
            raise CostPricingError(f"cost_pricing_duplicate_sku:{entry.sku}")
        entries[entry.sku] = entry

    with _CACHE_LOCK:
        _CACHED = entries
        _CACHED_PATH = target

    return dict(entries)


def get_price(
    sku: str,
    *,
    path: Optional[Path | str] = None,
) -> PriceEntry:
    """Return the ``PriceEntry`` for ``sku`` or raise ``CostPricingError``."""
    registry = load_pricing_registry(path=path)
    entry = registry.get(sku)
    if entry is None:
        raise CostPricingError(f"cost_pricing_unknown_sku:{sku}")
    return entry


def reset_pricing_cache() -> None:
    """Clear the cached registry. Tests use this between cases."""
    global _CACHED, _CACHED_PATH
    with _CACHE_LOCK:
        _CACHED = None
        _CACHED_PATH = None
