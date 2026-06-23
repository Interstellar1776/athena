#!/usr/bin/env python3
"""placeholders.py — the placeholder glossary for the narrative layer.

Build Sequence §19 step 4 (context doc §4/§14). This is the **single source of truth** for the named
blanks the LLM is allowed to reference instead of writing numbers. It answers three questions, and
nothing else does:

* **Which placeholders exist**, and which finding field each one pulls from (``PLACEHOLDERS``).
* **Which placeholders are legal for a given finding** — only those whose underlying field is present
  and non-null, so a CPA finding (no projection) is never offered ``{projected_linear}``
  (``available_placeholders``). This is what makes orphan placeholders structurally rare.
* **How to render a placeholder's number for display** — metric-aware ($ vs % vs count), because
  Python owns formatting too, not just the value (``render_placeholder``).

Why one file shared by two stages: the **generator** (step 4) shows the model this menu; the
**validator** (step 5) fills from the *same* table. One table ⇒ they can never disagree about what
``{variance_pct}`` means or how it should look.

Naming note: these are **placeholders**, never "tokens" — "token" means the model's text/billing unit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ===========================================================================
# 1. The placeholder spec + the registry
# ===========================================================================
@dataclass(frozen=True)
class PlaceholderSpec:
    """One placeholder's definition.

    ``source`` — where the value lives on the finding: a top-level key, or ``"supporting_metrics:<key>"``
    for a value nested in the finding's ``supporting_metrics`` bag.
    ``kind``   — the formatter to apply (see ``_format``), or the sentinel ``"metric"`` meaning *resolve
    the format from the finding's ``metric``* (so ``{actual}`` renders as $ on a CPA finding but as a
    plain count on a volume finding — see ``METRIC_FORMAT``).
    ``label``  — a short human description shown to the model in the prompt menu.
    """

    source: str
    kind: str
    label: str


# The placeholders. Value-bearing fields only — purely-alphabetic facts (entity, region, segment,
# risk_level, direction) are NOT placeholders: they contain no digits, so the model may write them
# freely. Anything that contains a digit (incl. the period and day counts) must be a placeholder.
PLACEHOLDERS: dict[str, PlaceholderSpec] = {
    # identity / timing (digit-bearing → must be placeholders)
    "period":             PlaceholderSpec("period", "month", "the period (month)"),
    "days_elapsed":       PlaceholderSpec("days_elapsed", "int", "days elapsed in the period"),
    "days_in_period":     PlaceholderSpec("days_in_period", "int", "total days in the period"),
    # the headline numbers (actual/reference/projection are metric-aware)
    "variance_pct":       PlaceholderSpec("variance_pct", "pct", "variance vs plan (already a percent)"),
    "actual":             PlaceholderSpec("actual", "metric", "the metric's actual value"),
    "reference":          PlaceholderSpec("reference_value", "metric", "the plan/reference value"),
    "projected_linear":   PlaceholderSpec("projected_period_end_linear", "metric", "linear period-end projection"),
    "projected_weighted": PlaceholderSpec("projected_period_end_weighted", "metric", "trend (weighted) period-end projection"),
    # economics context
    "cogs_per_unit":      PlaceholderSpec("cogs_per_unit", "currency", "cost of goods per unit"),
    "ltv":                PlaceholderSpec("ltv", "currency", "lifetime value"),
    "margin_per_unit":    PlaceholderSpec("margin_per_unit", "currency", "margin per unit"),
    # restatement (only present on restatement findings)
    "frozen_reference":   PlaceholderSpec("frozen_reference", "currency", "CPA frozen at period close"),
    "restatement_delta":  PlaceholderSpec("restatement_delta", "currency", "change from the late/accrued invoice"),
    # supporting_metrics — offered only when the key is actually present for that alert type
    "total_spend":        PlaceholderSpec("supporting_metrics:total_spend", "currency0", "total acquisition spend to date"),
    "conversions_landed": PlaceholderSpec("supporting_metrics:conversions_landed", "count", "conversions landed"),
    "cpa_t3m":            PlaceholderSpec("supporting_metrics:cpa_t3m", "currency", "trailing-3-month CPA"),
    "cpa_t12m":           PlaceholderSpec("supporting_metrics:cpa_t12m", "currency", "trailing-12-month CPA"),
    "submissions":        PlaceholderSpec("supporting_metrics:submissions", "count", "submissions (fallout denominator)"),
    "unmatched":          PlaceholderSpec("supporting_metrics:unmatched", "count", "unmatched submissions (fallout numerator)"),
    "trailing_baseline":  PlaceholderSpec("supporting_metrics:trailing_baseline", "rate", "the channel's trailing fallout baseline"),
    "price_per_unit":     PlaceholderSpec("supporting_metrics:price_per_unit", "currency", "price per unit"),
    "to_date":            PlaceholderSpec("supporting_metrics:to_date", "count", "volume to date"),
    "plan_prorated":      PlaceholderSpec("supporting_metrics:plan_prorated", "count", "pro-rated plan to date"),
}

# For metric-aware placeholders (kind="metric"): the format to use, keyed by the finding's metric.
# Calibrated against real findings — CPA/COGS/LTV/margin are dollars; volume is a count; fallout_rate
# is a 0–1 rate (×100 for display); the CPA-vs-LTV ratios are bare ratios.
METRIC_FORMAT: dict[str, str] = {
    "cost_per_acquisition": "currency",
    "volume_converted":     "count",
    "cogs_per_unit":        "currency",
    "margin_per_unit":      "currency",
    "ltv":                  "currency",
    "fallout_rate":         "rate",
    "t12m_cpa_over_ltv":    "ratio",
    "t3m_cpa_over_ltv":     "ratio",
}


# ===========================================================================
# 2. Value lookup + presence test
# ===========================================================================
def _value(finding: dict, source: str) -> Any:
    """Resolve a placeholder's raw value from the finding (top-level or nested in supporting_metrics)."""
    if source.startswith("supporting_metrics:"):
        key = source.split(":", 1)[1]
        return (finding.get("supporting_metrics") or {}).get(key)
    return finding.get(source)


def _present(value: Any) -> bool:
    """A value is offerable if it isn't None / NaN / empty-string."""
    if value is None or value == "":
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return True


# ===========================================================================
# 3. Formatting — Python owns how every number is displayed (§4)
# ===========================================================================
def _month(value: Any) -> str:
    """'2024-05' → 'May 2024' (falls back to the raw string if it isn't a YYYY-MM period)."""
    try:
        return datetime.strptime(str(value), "%Y-%m").strftime("%B %Y")
    except ValueError:
        return str(value)


def _format(value: Any, kind: str, finding: dict) -> str:
    """Render one value per its formatter kind. ``metric`` resolves via the finding's metric."""
    if kind == "metric":
        kind = METRIC_FORMAT.get(finding.get("metric", ""), "currency")
    if kind == "month":
        return _month(value)
    v = float(value)                                         # tolerate numpy floats / ints
    if kind == "pct":
        return f"{v:.1f}%"                                   # already in percentage points
    if kind == "rate":
        return f"{v * 100:.1f}%"                             # 0–1 rate → percent
    if kind == "currency":
        return f"${v:,.2f}"
    if kind == "currency0":
        return f"${v:,.0f}"
    if kind == "count":
        return f"{int(round(v)):,}"
    if kind == "int":
        return f"{int(round(v))}"
    if kind == "ratio":
        return f"{v:.2f}"
    return str(value)                                        # unknown kind → safe stringification


# ===========================================================================
# 4. Public API — used by the generator now, the validator (stage 5) later
# ===========================================================================
def available_placeholders(finding: dict) -> list[str]:
    """The placeholders that are legal for this finding (underlying field present & non-null).

    Offering only present placeholders is what keeps the model from referencing one that can't be
    filled — orphan prevention by construction. Order follows ``PLACEHOLDERS`` for stable prompts.
    """
    return [name for name, spec in PLACEHOLDERS.items() if _present(_value(finding, spec.source))]


def render_placeholder(name: str, finding: dict) -> str:
    """Format one placeholder's value for display. Raises if the placeholder is unknown or its value
    is absent on this finding (callers should pass names from ``available_placeholders``)."""
    spec = PLACEHOLDERS.get(name)
    if spec is None:
        raise KeyError(f"unknown placeholder: {{{name}}}")
    value = _value(finding, spec.source)
    if not _present(value):
        raise KeyError(f"placeholder {{{name}}} has no value on finding {finding.get('finding_id')}")
    return _format(value, spec.kind, finding)
