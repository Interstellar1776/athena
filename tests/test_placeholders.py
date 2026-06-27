"""Tests for the placeholder glossary (Build Sequence §19 step 4).

Pins the contract the generator and (later) the validator both depend on: which placeholders are legal
for a given finding, and how each value is formatted (metric-aware). Hermetic — synthetic findings,
no pipeline, no LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.llm.placeholders import (  # noqa: E402
    available_placeholders,
    render_placeholder,
)


def cpa_finding() -> dict:
    return {
        "finding_id": "F-001", "entity": "ERCOT", "region": "North", "segment": "Door_to_Door",
        "metric": "cost_per_acquisition", "alert_type": "cpa_spike", "period": "2024-05",
        "days_elapsed": 22, "days_in_period": 31, "confidence": "low", "estimated": True,
        "actual": 160.741, "reference_value": 125.0, "variance_pct": 28.59,
        "variance_direction": "UNFAVORABLE", "risk_level": "HIGH",
        "projected_period_end_linear": None, "projected_period_end_weighted": None,
        "cogs_per_unit": 44.0, "ltv": 264.0, "margin_per_unit": 43.95,
        "frozen_reference": None, "restatement_delta": None,
        "retrieved_context": "", "narrative": "",
        "supporting_metrics": {"cpa_t3m": 160.74, "cpa_t12m": 160.74, "total_spend": 13984.47,
                               "conversions_landed": 87},
    }


def volume_finding() -> dict:
    return {
        "finding_id": "F-002", "entity": "ERCOT", "region": "West", "segment": "Telemarketing",
        "metric": "volume_converted", "alert_type": "volume_miss", "period": "2024-05",
        "days_elapsed": 22, "days_in_period": 31, "confidence": "low", "estimated": True,
        "actual": 120.0312, "reference_value": 491.0, "variance_pct": -75.55,
        "variance_direction": "UNFAVORABLE", "risk_level": "HIGH",
        "projected_period_end_linear": 122.59, "projected_period_end_weighted": 120.03,
        "supporting_metrics": {"to_date": 37.0, "plan_prorated": 156.8},
    }


def fallout_finding() -> dict:
    return {
        "finding_id": "F-003", "entity": "ERCOT", "region": "West", "segment": "Telemarketing",
        "metric": "fallout_rate", "alert_type": "fallout_rate", "period": "2023-12",
        "days_elapsed": 31, "days_in_period": 31, "confidence": "high", "estimated": False,
        "actual": 0.2184, "reference_value": 0.1275, "variance_pct": 71.29,
        "variance_direction": "UNFAVORABLE", "risk_level": "HIGH",
        "supporting_metrics": {"submissions": 174, "unmatched": 38, "trailing_baseline": 0.1275},
    }


# ---------------------------------------------------------------------------
# available_placeholders — only present fields are offered (orphan prevention)
# ---------------------------------------------------------------------------
def test_cpa_offers_present_placeholders_only():
    avail = available_placeholders(cpa_finding())
    assert {"actual", "reference", "variance_pct", "cogs_per_unit", "ltv", "period",
            "total_spend", "conversions_landed", "cpa_t3m"} <= set(avail)
    # CPA isn't projected and isn't a restatement → those placeholders must NOT be offered.
    assert "projected_linear" not in avail
    assert "frozen_reference" not in avail
    assert "submissions" not in avail


def test_volume_offers_projection_placeholders():
    avail = available_placeholders(volume_finding())
    assert "projected_linear" in avail and "projected_weighted" in avail
    assert "to_date" in avail


# ---------------------------------------------------------------------------
# render_placeholder — metric-aware formatting (Python owns display)
# ---------------------------------------------------------------------------
def test_currency_and_percent_and_period_formatting():
    f = cpa_finding()
    assert render_placeholder("actual", f) == "$160.74"
    assert render_placeholder("reference", f) == "$125.00"
    assert render_placeholder("variance_pct", f) == "28.6%"
    assert render_placeholder("period", f) == "May 2024"
    assert render_placeholder("days_elapsed", f) == "22"
    assert render_placeholder("total_spend", f) == "$13,984"
    assert render_placeholder("conversions_landed", f) == "87"


def test_volume_metric_renders_as_count_not_currency():
    f = volume_finding()
    assert render_placeholder("actual", f) == "120"          # rounded count, no $, no decimals
    assert render_placeholder("reference", f) == "491"
    assert render_placeholder("projected_linear", f) == "123"


def test_fallout_rate_renders_as_percent_times_100():
    f = fallout_finding()
    assert render_placeholder("actual", f) == "21.8%"        # 0.2184 → 21.8%
    assert render_placeholder("reference", f) == "12.8%"     # 0.1275 → 12.8%
    assert render_placeholder("trailing_baseline", f) == "12.8%"


# ---------------------------------------------------------------------------
# render_placeholder — guards
# ---------------------------------------------------------------------------
def test_render_raises_on_absent_or_unknown():
    f = cpa_finding()
    with pytest.raises(KeyError):
        render_placeholder("projected_linear", f)            # absent on a CPA finding
    with pytest.raises(KeyError):
        render_placeholder("made_up_metric", f)              # not in the registry
