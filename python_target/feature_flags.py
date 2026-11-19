"""
Feature flags for the phased ABAP → Python cutover.

Each migrated object has its own flag so it can move independently through
    legacy (ABAP)  →  shadow / validate_only  →  python  →  flag removed

Resolution order (first hit wins):
    1. ``config`` dict passed by the caller
    2. Environment variables (``FF_<FLAG>_MODE`` + optional ``FF_<FLAG>_<ATTR>``)
    3. Legacy default — nothing changes unless a flag is explicitly enabled

Config dict format::

    {
        "vendor_lookup": {
            "default": "abap",
            "rules": [
                {"mode": "python", "match": {"bukrs": ["1000"]}},
                {"mode": "shadow", "match": {"bukrs": ["2000"], "lifnr": ["0000100001"]}},
            ],
        }
    }

Environment format (mode applies only to the cohort when attribute vars are set)::

    FF_VENDOR_LOOKUP_MODE=python
    FF_VENDOR_LOOKUP_BUKRS=1000,2000
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Generic, Mapping, Optional, TypeVar

logger = logging.getLogger(__name__)


class InventoryReportFlagMode(str, Enum):
    """Z_INVENTORY_REPORT routing modes."""

    ABAP = "abap"
    SHADOW = "shadow"
    PYTHON = "python"


class VendorLookupFlagMode(str, Enum):
    """Z_RFC_VENDOR_LOOKUP routing modes."""

    ABAP = "abap"
    SHADOW = "shadow"
    PYTHON = "python"


class OrderSyncFlagMode(str, Enum):
    """Z_IDOC_ORDER_SYNC routing modes."""

    IDOC_ABAP = "idoc_abap"
    VALIDATE_ONLY = "validate_only"
    PYTHON = "python"


INVENTORY_REPORT = "inventory_report"
VENDOR_LOOKUP = "vendor_lookup"
ORDER_SYNC = "order_sync"

FLAG_MODES: dict[str, type[Enum]] = {
    INVENTORY_REPORT: InventoryReportFlagMode,
    VENDOR_LOOKUP: VendorLookupFlagMode,
    ORDER_SYNC: OrderSyncFlagMode,
}

LEGACY_MODES: dict[str, Enum] = {
    INVENTORY_REPORT: InventoryReportFlagMode.ABAP,
    VENDOR_LOOKUP: VendorLookupFlagMode.ABAP,
    ORDER_SYNC: OrderSyncFlagMode.IDOC_ABAP,
}

# Attributes each flag can be scoped by (SAP field names, lower-case).
TARGETING_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    INVENTORY_REPORT: ("werks", "bukrs"),
    VENDOR_LOOKUP: ("bukrs", "lifnr"),
    ORDER_SYNC: ("docnum", "sndprn", "kunnr"),
}

ENV_PREFIX = "FF_"

Attributes = Mapping[str, Any]


class LegacyHandlerRequired(RuntimeError):
    """Raised when a mode needs the legacy (ABAP) path but no handler was injected."""

    def __init__(self, flag: str, mode: Enum) -> None:
        super().__init__(f"Flag {flag}={mode.value} requires a legacy_handler")


@dataclass(frozen=True)
class FlagDecision:
    """Resolved mode for one call, plus why it was chosen."""

    flag: str
    mode: Enum
    reason: str
    attributes: dict[str, Any] = field(default_factory=dict)


def _parse_mode(flag: str, raw: Any) -> Optional[Enum]:
    if isinstance(raw, FLAG_MODES[flag]):
        return raw
    try:
        return FLAG_MODES[flag](str(raw).strip().lower())
    except ValueError:
        logger.warning("Invalid mode %r for flag %s; using legacy mode", raw, flag)
        return None


def _matches(match: Mapping[str, Any], attributes: Attributes) -> bool:
    """True when every match key is satisfied.

    A list-valued attribute (e.g. several plants in one report) matches only
    if every value is in the cohort, so a mixed request stays on legacy.
    """
    for key, allowed in match.items():
        allowed_set = (
            {str(v).strip() for v in allowed}
            if isinstance(allowed, (list, tuple, set, frozenset))
            else {str(allowed).strip()}
        )
        value = attributes.get(key)
        if value is None:
            return False
        values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        values = [str(v).strip() for v in values]
        if not values or not all(v in allowed_set for v in values):
            return False
    return True


def _config_from_env(flag: str, env: Mapping[str, str]) -> Optional[dict]:
    mode = env.get(f"{ENV_PREFIX}{flag.upper()}_MODE")
    if not mode:
        return None
    match = {}
    for attr in TARGETING_ATTRIBUTES[flag]:
        raw = env.get(f"{ENV_PREFIX}{flag.upper()}_{attr.upper()}")
        if raw:
            match[attr] = [v for v in raw.split(",") if v.strip()]
    if match:
        return {"rules": [{"mode": mode, "match": match}]}
    return {"default": mode}


def resolve_flag(
    flag: str,
    attributes: Optional[Attributes] = None,
    config: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> FlagDecision:
    """Resolve the mode for ``flag`` given the call's targeting attributes."""
    if flag not in FLAG_MODES:
        raise KeyError(f"Unknown feature flag: {flag}")

    attrs = dict(attributes or {})
    env = os.environ if env is None else env
    legacy = LEGACY_MODES[flag]

    source = "config"
    flag_config = (config or {}).get(flag)
    if flag_config is None:
        source = "env"
        flag_config = _config_from_env(flag, env)
    if flag_config is None:
        return FlagDecision(flag, legacy, "default", attrs)

    for index, rule in enumerate(flag_config.get("rules", [])):
        if _matches(rule.get("match", {}), attrs):
            mode = _parse_mode(flag, rule.get("mode"))
            if mode is not None:
                return FlagDecision(flag, mode, f"{source}:rule[{index}]", attrs)
            return FlagDecision(flag, legacy, f"{source}:rule[{index}]:invalid_mode", attrs)

    if "default" in flag_config:
        mode = _parse_mode(flag, flag_config["default"])
        if mode is not None:
            return FlagDecision(flag, mode, f"{source}:default", attrs)

    return FlagDecision(flag, legacy, f"{source}:no_match", attrs)


# ---------------------------------------------------------------------------
# Shadow comparison + observability
# ---------------------------------------------------------------------------

@dataclass
class FieldDiff:
    """One field that differs between the legacy and Python results."""

    key: str
    field: str
    legacy: Any
    python: Any


@dataclass
class ShadowDiff:
    """Structured comparison of a shadowed call."""

    differences: list[FieldDiff] = field(default_factory=list)
    python_error: Optional[str] = None

    @property
    def clean(self) -> bool:
        return not self.differences and self.python_error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "difference_count": len(self.differences),
            "differences": [asdict(d) for d in self.differences],
            "python_error": self.python_error,
        }


T = TypeVar("T")


@dataclass
class RoutedResult(Generic[T]):
    """What a routed call returns: the served result and how it was produced."""

    result: T
    decision: FlagDecision
    served_by: str
    diff: Optional[ShadowDiff] = None
    details: dict[str, Any] = field(default_factory=dict)


def emit_flag_event(routed: RoutedResult) -> dict[str, Any]:
    """Log one structured event per routed call (JSON message + ``extra``)."""
    event: dict[str, Any] = {
        "event": "feature_flag.route",
        "flag": routed.decision.flag,
        "mode": routed.decision.mode.value,
        "reason": routed.decision.reason,
        "attributes": routed.decision.attributes,
        "served_by": routed.served_by,
        "diff": routed.diff.to_dict() if routed.diff is not None else None,
        **routed.details,
    }
    level = logging.WARNING if routed.diff is not None and not routed.diff.clean else logging.INFO
    logger.log(level, json.dumps(event, default=str), extra={"flag_event": event})
    return event
