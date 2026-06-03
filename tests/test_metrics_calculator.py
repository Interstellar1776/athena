"""Tests for the metrics calculator (Build Sequence §19 step 3, module 1).

The calculator computes the four metrics + fallout and labels each with its method, so these
tests pin the things that make those *correct and honestly labeled*: native grain per frame,
reconciliation to the source (CPA = spend ÷ conversions; fallout = unmatched ÷ submissions;
COGS = the effective config rate), the fallback hierarchies producing the right labels, the
estimated flag tracking method, projectability tracking the current period, and no inf/NaN
leaking from a zero denominator.

Runs against the real configured snapshot (data/snapshots/2024-05-22) through the real
loader/merger/gl_processor, so the whole upstream chain is exercised on the way in.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.data_loader import load_data                       # noqa: E402
from app.analytics.data_merger import DIMS, merge_frames             # noqa: E402
from app.analytics.gl_processor import UNIT, process_gl             # noqa: E402
from app.analytics import metrics_calculator as mc                  # noqa: E402

SNAPSHOT_DATE = dt.date(2024, 5, 22)
SNAP_PERIOD = "2024-05"
PERIOD_CLOSE_DAY = 8


@pytest.fixture(scope="module")
def loaded():
    return load_data()


@pytest.fixture(scope="module")
def frames(loaded):
    merged = merge_frames(loaded)
    gl_states = process_gl(merged["gl_acquisition"])
    return mc.calculate_metrics(merged, gl_states, loaded["cogs_config"],
                                loaded["retention_config"],
                                snapshot_date=SNAPSHOT_DATE, period_close_day=PERIOD_CLOSE_DAY)


# ---------------------------------------------------------------------------
# Shape / grain — each metric at its honest grain, never collapsed
# ---------------------------------------------------------------------------
def test_returns_three_frames(frames):
    assert set(frames) == {"cpa", "economics", "fallout"}


def test_cpa_is_unit_grain(frames):
    cpa = frames["cpa"]
    assert not cpa.duplicated(UNIT + ["period"]).any()          # one row per unit×period
    assert len(UNIT) == 3


def test_economics_and_fallout_are_leaf_grain(frames):
    for key in ("economics", "fallout"):
        df = frames[key]
        assert not df.duplicated(DIMS + ["period"]).any()       # one row per leaf×period
        assert set(DIMS) <= set(df.columns)


# ---------------------------------------------------------------------------
# Reconciliation — numbers trace to source
# ---------------------------------------------------------------------------
def test_cpa_real_reconciles_to_spend_over_conversions(frames):
    """For a `real` CPA row, cpa_monthly must equal total_spend ÷ conversions_landed exactly."""
    cpa = frames["cpa"]
    real = cpa[cpa["cpa_monthly_method"] == mc.CPA_REAL]
    assert len(real) > 0
    expected = real["total_spend"] / real["conversions_landed"]
    assert np.allclose(real["cpa_monthly"], expected, rtol=1e-9)


def test_fallout_rate_reconciles(frames):
    fo = frames["fallout"]
    expected = fo["unmatched"] / fo["submissions"]
    assert np.allclose(fo["fallout_rate"], expected, equal_nan=True)
    assert (fo["unmatched"] == fo["submissions"] - fo["matched"]).all()


def test_cogs_actual_matches_effective_config_rate(frames, loaded):
    """An `actual` COGS equals the config rate whose effective_date is on/before period-end."""
    econ = frames["economics"]
    cfg = loaded["cogs_config"].copy()
    cfg["_eff"] = cfg["effective_date"]
    sample = econ[econ["cogs_method"] == mc.COGS_ACTUAL].iloc[0]
    row = cfg.merge(sample[DIMS].to_frame().T, on=DIMS, how="inner")
    eligible = row[row["_eff"] <= mc._period_end(sample["period"]).isoformat()]
    assert float(eligible.sort_values("_eff").iloc[-1]["cogs_per_unit"]) == sample["cogs_per_unit"]


# ---------------------------------------------------------------------------
# Method labels — the fallback hierarchies (§9–§10)
# ---------------------------------------------------------------------------
def test_open_period_cpa_is_gl_partial_and_projectable(frames):
    """At the May-22 snapshot, May units with posted spend are open → gl_partial + projectable."""
    cpa = frames["cpa"]
    open_may = cpa[(cpa["period"] == SNAP_PERIOD) &
                   (cpa["gl_completeness_state"] == "open")]
    assert len(open_may) > 0
    assert (open_may["cpa_monthly_method"] == mc.CPA_GL_PARTIAL).all()
    assert open_may["is_projectable"].all()
    assert open_may["cpa_estimated"].all()


def test_cpa_method_values_are_in_vocabulary(frames):
    allowed = {mc.CPA_REAL, mc.CPA_GL_PARTIAL, mc.CPA_TRAILING, mc.CPA_PLAN, mc.LTV_UNRESOLVED}
    assert set(frames["cpa"]["cpa_monthly_method"]) <= allowed


def test_ltv_first_months_fall_back_to_plan(frames):
    """A leaf's earliest periods lack 3 months of margin history → plan_input, not calculated."""
    econ = frames["economics"].sort_values(DIMS + ["period"])
    first = econ.groupby(DIMS, observed=True).head(1)
    assert (first["ltv_method"] == mc.LTV_PLAN).mean() > 0.5     # most first-months are plan
    assert set(econ["ltv_method"]) <= {mc.LTV_RETENTION, mc.LTV_TERM,
                                       mc.LTV_PLAN, mc.LTV_UNRESOLVED}


def test_margin_plan_fallback_only_when_price_missing(frames):
    econ = frames["economics"]
    plan = econ[econ["margin_method"] == mc.MARGIN_PLAN]
    calc = econ[econ["margin_method"] == mc.MARGIN_CALC]
    assert plan["price_per_unit"].isna().all()                  # plan fallback ⇒ no price
    assert calc["price_per_unit"].notna().all()                 # calculated ⇒ price present


# ---------------------------------------------------------------------------
# Estimated flag (orthogonal to severity) + projectability
# ---------------------------------------------------------------------------
def test_estimated_flag_tracks_method(frames):
    cpa, econ = frames["cpa"], frames["economics"]
    assert (cpa["cpa_estimated"] == (cpa["cpa_monthly_method"] != mc.CPA_REAL)).all()
    assert (econ["margin_estimated"] == (econ["margin_method"] != mc.MARGIN_CALC)).all()
    calc_ltv = econ["ltv_method"].isin([mc.LTV_RETENTION, mc.LTV_TERM])
    assert (econ["ltv_estimated"] == ~calc_ltv).all()


def test_is_projectable_is_only_the_current_period(frames):
    cpa, econ = frames["cpa"], frames["economics"]
    assert (cpa["is_projectable"] == (cpa["period"] == SNAP_PERIOD)).all()
    assert (econ["is_projectable"] == (econ["period"] == SNAP_PERIOD)).all()


# ---------------------------------------------------------------------------
# No bogus numbers — a zero denominator must never leak inf
# ---------------------------------------------------------------------------
def test_no_infinities(frames):
    for key, df in frames.items():
        num = df.select_dtypes(include="number")
        assert not np.isinf(num.to_numpy()).any(), f"inf leaked into {key}"
