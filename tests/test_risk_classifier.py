"""Tests for the risk classifier (Build Sequence §19 step 3, module 3).

The classifier scores every metric, so these tests pin what makes that honest: a normalized
schema, every row carrying a level, severity that is magnitude-only (an estimated HIGH stays
HIGH), the `estimated` flag tracking the input method, the metric-driven grain, the no-cry-wolf
guards (pending fallout, first-run volume), and the headline demo signals landing at May-22.

Runs against the real configured snapshot (2024-05-22) through the full upstream chain.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics import risk_classifier as rc                  # noqa: E402
from app.analytics.risk_classifier import ASSESSMENT_COLUMNS, LEAF_ONLY  # noqa: E402

SNAP_PERIOD = "2024-05"


@pytest.fixture(scope="module")
def assessments():
    return rc.compute_assessments()["assessments"]


# ---------------------------------------------------------------------------
# Schema / completeness — every metric carries a level
# ---------------------------------------------------------------------------
def test_schema_is_uniform(assessments):
    assert list(assessments.columns) == ASSESSMENT_COLUMNS


def test_every_row_has_a_level(assessments):
    assert assessments["risk_level"].isin({rc.HIGH, rc.MEDIUM, rc.LOW, rc.INFO}).all()
    assert assessments["risk_level"].notna().all()


def test_low_dominates_high_is_rare(assessments):
    counts = assessments["risk_level"].value_counts()
    assert counts.get(rc.LOW, 0) > counts.get(rc.HIGH, 0)       # most metrics are on-track


# ---------------------------------------------------------------------------
# Severity is magnitude-only; estimated is orthogonal
# ---------------------------------------------------------------------------
def test_estimated_high_exists_and_is_not_downgraded(assessments):
    """At May-22 the CPA spikes are estimated (open-period gl_partial) yet still HIGH."""
    spikes = assessments[(assessments["alert_type"] == "cpa_spike") &
                         (assessments["risk_level"] == rc.HIGH)]
    assert spikes["estimated"].all()                            # estimated ...
    assert len(spikes) >= 2                                     # ... and still HIGH


def test_estimated_tracks_method(assessments):
    a = assessments
    # gl_partial CPA → estimated; calculated margin / actual COGS → not estimated.
    cpa = a[(a["alert_type"] == "cpa_spike") & (a["actual_method"] == "gl_partial")]
    assert cpa["estimated"].all()
    margin = a[(a["alert_type"] == "margin_compression") & (a["actual_method"] == "calculated")]
    assert (~margin["estimated"]).all()


# ---------------------------------------------------------------------------
# Headline demo signals land
# ---------------------------------------------------------------------------
def test_cpa_spike_high_for_the_two_spiking_units(assessments):
    hi = assessments[(assessments["alert_type"] == "cpa_spike") &
                     (assessments["risk_level"] == rc.HIGH) & (assessments["period"] == SNAP_PERIOD)]
    pairs = set(zip(hi["region"], hi["segment"]))
    assert ("North", "Door_to_Door") in pairs
    assert ("West", "Telemarketing") in pairs


def test_margin_compression_high_for_the_cogs_anomaly_unit(assessments):
    hi = assessments[(assessments["alert_type"] == "margin_compression") &
                     (assessments["risk_level"] == rc.HIGH)]
    assert (("North", "Online_Partner") in set(zip(hi["region"], hi["segment"])))
    assert (hi["variance_direction"] == rc.UNFAVORABLE).all()


def test_cpa_ltv_inversion_is_dormant_not_fabricated(assessments):
    inv = assessments[assessments["alert_type"] == "cpa_ltv_inversion"]
    assert len(inv) > 0 and (inv["risk_level"] == rc.LOW).all()   # honestly nothing crosses


# ---------------------------------------------------------------------------
# No cry-wolf — pending fallout + first-run volume are not hard alerts
# ---------------------------------------------------------------------------
def test_current_pending_fallout_is_not_banded(assessments):
    """The current month's fallout is pending (unresolved) at May-22 → LOW + estimated."""
    cur = assessments[(assessments["alert_type"] == "fallout_rate") &
                      (assessments["period"] == SNAP_PERIOD)]
    assert (cur["risk_level"] == rc.LOW).all()
    assert cur["estimated"].all()


def test_first_run_segment_volume_miss_not_high(assessments):
    """Telemarketing West launched mid-May (no history) → its volume_miss must not be HIGH."""
    vm = assessments[assessments["alert_type"].str.startswith("volume_miss") &
                     (assessments["segment"] == "Telemarketing") & (assessments["region"] == "West")]
    assert (vm["risk_level"] != rc.HIGH).all()


# ---------------------------------------------------------------------------
# Grain is metric-driven
# ---------------------------------------------------------------------------
def test_unit_alerts_blank_leaf_dims_leaf_alerts_dont(assessments):
    unit = assessments[assessments["grain"] == "unit"]
    leaf = assessments[assessments["grain"] == "leaf"]
    assert (unit[LEAF_ONLY] == "").all().all()                  # unit rows blank the leaf-only dims
    assert (leaf["service_territory"].str.len() > 0).all()      # leaf rows carry them
    assert assessments["group_key"].str.count(r"\|").eq(2).all()  # entity|region|segment


# ---------------------------------------------------------------------------
# Restatement is derived statelessly
# ---------------------------------------------------------------------------
def test_restatement_delta_is_late_over_conversions(assessments):
    r = assessments[assessments["alert_type"] == "restatement"]
    assert len(r) >= 1
    row = r.iloc[0]
    # frozen_reference = actual − restatement_delta, and delta > 0 (spend added late).
    assert row["reference_type"] == "frozen_reference"
    assert row["reference_value"] < row["actual"]


# ---------------------------------------------------------------------------
# No bogus numbers
# ---------------------------------------------------------------------------
def test_no_infinities(assessments):
    num = assessments[["actual", "reference_value", "variance_pct"]].apply(
        lambda c: c.map(lambda v: v if isinstance(v, (int, float)) else np.nan))
    assert not np.isinf(num.to_numpy(dtype=float)).any()
