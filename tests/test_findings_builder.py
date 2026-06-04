"""Tests for the findings builder (Build Sequence §19 step 3, module 4).

The builder turns the assessment table into the §14 structured findings, so these tests pin the
contract: the full §14 key set on every finding, only non-LOW become findings, multi-leaf events
roll into one finding with the leaf breakdown nested, the unit's economic context is attached,
restatement carries its derived fields, the feed is ranked, and ids are deterministic.

Runs against the real configured snapshot (2024-05-22) through the full chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics import findings_builder as fb                  # noqa: E402
from app.analytics.findings_builder import SEVERITY_RANK          # noqa: E402
from app.analytics.risk_classifier import HIGH, LOW, MEDIUM, INFO  # noqa: E402

SNAP_PERIOD = "2024-05"

# Every key the §14 contract requires (docs/athena_context.md §14).
SECTION_14_KEYS = {
    "finding_id", "entity", "region", "segment", "product_type", "metric", "period",
    "days_elapsed", "days_in_period", "confidence", "is_projectable", "actual", "actual_method",
    "reference_value", "reference_type", "variance_pct", "variance_direction", "risk_level",
    "estimated", "projected_period_end_linear", "projected_period_end_weighted", "cogs_per_unit",
    "cogs_method", "ltv", "ltv_method", "margin_per_unit", "margin_method", "unit_economics_flag",
    "gl_completeness_state", "frozen_reference", "restatement_delta", "supporting_metrics",
    "retrieved_context", "narrative", "validated", "validation_flags"}


@pytest.fixture(scope="module")
def built():
    return fb.compute_findings()


@pytest.fixture(scope="module")
def findings(built):
    return built["findings"]


# ---------------------------------------------------------------------------
# Contract / selection
# ---------------------------------------------------------------------------
def test_every_finding_has_the_section_14_keys(findings):
    assert len(findings) > 0
    for f in findings:
        assert SECTION_14_KEYS <= set(f), f"missing {SECTION_14_KEYS - set(f)}"


def test_only_non_low_become_findings(findings):
    assert all(f["risk_level"] in {HIGH, MEDIUM, INFO} for f in findings)
    assert not any(f["risk_level"] == LOW for f in findings)


def test_assessment_table_is_the_browse_layer(built):
    """The full assessment table (incl. LOW) rides alongside the findings for drill-down."""
    a = built["assessments"]
    assert (a["risk_level"] == LOW).any()                    # LOW lives in the table, not the feed
    assert len(a) > len(built["findings"])


def test_downstream_slots_are_empty(findings):
    for f in findings:
        assert f["narrative"] == "" and f["retrieved_context"] == ""
        assert f["validated"] is False and f["validation_flags"] == []


# ---------------------------------------------------------------------------
# Roll-up — one finding per (alert_type, unit, period), leaves nested
# ---------------------------------------------------------------------------
def test_cogs_anomaly_is_one_finding_with_three_leaves(findings):
    cogs = [f for f in findings if f["alert_type"] == "cogs_spike"
            and f["segment"] == "Online_Partner" and f["region"] == "North"]
    assert len(cogs) == 1                                     # rolled, not 3 separate findings
    assert cogs[0]["supporting_metrics"]["leaf_count"] == 3
    assert len(cogs[0]["supporting_metrics"]["leaves"]) == 3


def test_rolled_unit_finding_blanks_leaf_dims(findings):
    cogs = next(f for f in findings if f["alert_type"] == "cogs_spike"
                and f["segment"] == "Online_Partner")
    assert cogs["product_type"] == "" and cogs["customer_class"] == ""   # spread lives in leaves


# ---------------------------------------------------------------------------
# Context bundling + derived fields
# ---------------------------------------------------------------------------
def test_economic_context_attached(findings):
    spike = next(f for f in findings if f["alert_type"] == "cpa_spike"
                 and f["segment"] == "Door_to_Door" and f["region"] == "North")
    assert spike["cogs_method"] is not None and spike["ltv_method"] is not None
    assert spike["margin_method"] is not None


def test_restatement_carries_frozen_and_delta(findings):
    r = [f for f in findings if f["alert_type"] == "restatement"]
    assert len(r) >= 1
    assert r[0]["frozen_reference"] is not None and r[0]["restatement_delta"] is not None
    # delta = actual − frozen
    assert abs((r[0]["actual"] - r[0]["frozen_reference"]) - r[0]["restatement_delta"]) < 0.01


def test_estimated_set_on_open_period_cpa_spike(findings):
    spike = next(f for f in findings if f["alert_type"] == "cpa_spike"
                 and f["segment"] == "Door_to_Door" and f["period"] == SNAP_PERIOD)
    assert spike["estimated"] is True                        # gl_partial in the open period


# ---------------------------------------------------------------------------
# Ranking + ids
# ---------------------------------------------------------------------------
def test_feed_ranked_by_severity(findings):
    sev = [SEVERITY_RANK[f["risk_level"]] for f in findings]
    assert sev == sorted(sev)                                # HIGH block, then MEDIUM, then INFO
    assert findings[0]["risk_level"] == HIGH


def test_finding_ids_sequential_and_deterministic():
    a = fb.compute_findings()["findings"]
    b = fb.compute_findings()["findings"]
    assert [f["finding_id"] for f in a] == [f"F-{i:03d}" for i in range(1, len(a) + 1)]
    key = lambda fs: [(f["finding_id"], f["alert_type"], f["entity"], f["region"],
                       f["segment"], f["period"]) for f in fs]
    assert key(a) == key(b)
