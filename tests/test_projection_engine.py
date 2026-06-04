"""Tests for the projection engine (Build Sequence §19 step 3, module 2).

The engine projects period-end volume for the current period only, so these tests pin the
things that make those projections correct and honest: leaf grain, current-period-only,
linear reconciliation, plan pro-rating, the regression's monotonic floor + linear fallback,
confidence bucketing, and no inf/NaN.

Runs against the real configured snapshot (data/snapshots/2024-05-22) through the real
loader/merger/gl_processor/metrics chain.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.data_loader import load_data                       # noqa: E402
from app.analytics.data_merger import DIMS, merge_frames             # noqa: E402
from app.analytics.gl_processor import process_gl                    # noqa: E402
from app.analytics.metrics_calculator import calculate_metrics       # noqa: E402
from app.analytics import projection_engine as pe                    # noqa: E402

SNAPSHOT_DATE = dt.date(2024, 5, 22)
SNAP_PERIOD = "2024-05"
DAYS_IN, DAYS_ELAPSED = 31, 22                                        # calendar basis, May @ 22nd


@pytest.fixture(scope="module")
def projection():
    data = load_data()
    merged = merge_frames(data)
    gl = process_gl(merged["gl_acquisition"])
    metrics = calculate_metrics(merged, gl, data["cogs_config"], data["retention_config"],
                                snapshot_date=SNAPSHOT_DATE, period_close_day=8)
    return pe.project_volume(data, merged, metrics, snapshot_date=SNAPSHOT_DATE,
                             pro_rate=pe.CALENDAR)["volume_projection"]


# ---------------------------------------------------------------------------
# Grain / scope
# ---------------------------------------------------------------------------
def test_leaf_grain_one_row_per_leaf(projection):
    assert not projection.duplicated(DIMS).any()
    assert set(DIMS) <= set(projection.columns)


def test_only_current_period_projected(projection):
    assert (projection["period"] == SNAP_PERIOD).all()


def test_day_counts_match_calendar_basis(projection):
    assert (projection["days_in_period"] == DAYS_IN).all()
    assert (projection["days_elapsed"] == DAYS_ELAPSED).all()
    assert (projection["days_basis"] == pe.CALENDAR).all()


# ---------------------------------------------------------------------------
# Reconciliation — linear pace + plan pro-rating
# ---------------------------------------------------------------------------
def test_linear_projection_is_pace_to_date_scaled(projection):
    for metric in ("converted", "submissions"):
        expected = projection[f"{metric}_to_date"] * DAYS_IN / DAYS_ELAPSED
        assert np.allclose(projection[f"{metric}_proj_linear"], expected, rtol=1e-9)


def test_plan_prorated_is_full_plan_scaled(projection):
    for metric in ("converted", "submissions"):
        expected = projection[f"{metric}_plan_full"] * DAYS_ELAPSED / DAYS_IN
        assert np.allclose(projection[f"{metric}_plan_prorated"], expected, rtol=1e-9)


def test_plan_full_present_for_every_leaf(projection):
    assert projection["converted_plan_full"].notna().all()


# ---------------------------------------------------------------------------
# Method labels + the monotonic floor
# ---------------------------------------------------------------------------
def test_regression_used_at_high_elapsed(projection):
    # 22 of 31 days elapsed ⇒ the trailing-21-day window is full.
    assert (projection["converted_proj_method"] == pe.M_REG_21D).all()
    assert (projection["confidence"] == pe.CONF_HIGH).all()


def test_projections_never_below_to_date(projection):
    """Cumulative is non-decreasing, so both projection lines must be ≥ the to-date value
    (linear scales up; the OLS slope of a monotonic series is ≥ 0)."""
    for metric in ("converted", "submissions"):
        td = projection[f"{metric}_to_date"]
        assert (projection[f"{metric}_proj_linear"] >= td - 1e-9).all()
        assert (projection[f"{metric}_proj_weighted"] >= td - 1e-9).all()


# ---------------------------------------------------------------------------
# Unit-level behaviour of the projector (fallback + confidence)
# ---------------------------------------------------------------------------
def test_single_point_falls_back_to_linear():
    period = pd.Period("2024-05", freq="M")
    daily = pd.Series({pd.Timestamp("2024-05-01"): 10})
    out = pe._project_series(daily, period=period, snapshot_date=dt.date(2024, 5, 1),
                             basis=pe.CALENDAR, days_in=31, days_elapsed=1, days_remaining=30)
    assert out["proj_method"] == pe.M_LINEAR
    assert out["proj_weighted"] == out["proj_linear"] == pytest.approx(10 * 31 / 1)


def test_confidence_buckets():
    assert pe._confidence(5, 31) == pe.CONF_LOW       # ~0.16
    assert pe._confidence(15, 31) == pe.CONF_MED      # ~0.48
    assert pe._confidence(22, 31) == pe.CONF_HIGH     # ~0.71


def test_business_day_counts_differ_from_calendar():
    cal = pe._day_counts(SNAPSHOT_DATE, pe.CALENDAR)
    bus = pe._day_counts(SNAPSHOT_DATE, pe.BUSINESS)
    assert cal == (31, 22, 9)
    assert bus[0] < cal[0] and bus[1] < cal[1]        # fewer business days than calendar days


# ---------------------------------------------------------------------------
# No bogus numbers
# ---------------------------------------------------------------------------
def test_no_infinities(projection):
    num = projection.select_dtypes(include="number")
    assert not np.isinf(num.to_numpy()).any()
    assert not num.isna().to_numpy().any()


# ---------------------------------------------------------------------------
# Proactive fallout (resolved sub-cohort)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fallout_projection():
    data = load_data()
    metrics = calculate_metrics(merge_frames(data), process_gl(merge_frames(data)["gl_acquisition"]),
                                data["cogs_config"], data["retention_config"],
                                snapshot_date=SNAPSHOT_DATE, period_close_day=8)
    return pe.project_fallout(data, metrics, snapshot_date=SNAPSHOT_DATE)["fallout_projection"]


def test_fallout_projection_is_current_period_leaf_grain(fallout_projection):
    fp = fallout_projection
    assert (fp["period"] == SNAP_PERIOD).all()
    assert not fp.duplicated(DIMS).any()
    assert ((fp["fallout_rate"] >= 0) & (fp["fallout_rate"] <= 1)).all()


def test_fallout_method_and_confidence_are_consistent(fallout_projection):
    fp = fallout_projection
    # Thin resolved sub-cohorts fall back to the plain rate, labeled no_data.
    no_data = fp[fp["fallout_method"] == "plain_no_data"]
    assert (no_data["confidence"] == "no_data").all()
    subcohort = fp[fp["fallout_method"] == "resolved_subcohort"]
    assert subcohort["confidence"].isin({"low", "medium", "high"}).all()
    assert (subcohort["resolved_sales"] >= pe.MIN_RESOLVED_SALES).all()
