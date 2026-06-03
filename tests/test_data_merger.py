"""Tests for the data merger (Build Sequence §19 step 3).

The merger only joins, aggregates, and counts — so these tests pin the things that make
those operations *correct*: no row explosion, totals that reconcile to the source feeds,
the right grain on each frame, overhead excluded from the acquisition view, forecast kept
accessible, and the string-key join actually matching Month-to-Month rows.

Runs against the real configured snapshot (data/snapshots/2024-05-22) loaded through the
real loader, so the gate + typing are exercised on the way in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.data_loader import load_data            # noqa: E402
from app.analytics.data_merger import DIMS, merge_frames   # noqa: E402


@pytest.fixture(scope="module")
def loaded():
    return load_data()


@pytest.fixture(scope="module")
def merged(loaded):
    return merge_frames(loaded)


# ---------------------------------------------------------------------------
# Shape / contract
# ---------------------------------------------------------------------------
def test_returns_expected_keys(merged):
    assert set(merged) == {"sales_with_ref", "conversions_with_ref", "fallout",
                           "gl_full", "gl_acquisition", "unmatched_sales", "unmatched_gl"}


def test_no_row_explosion(loaded, merged):
    # The whole point: actuals collapse to leaf×period, not the 156k record grain.
    n_groups = loaded["sales"].assign(
        period=loaded["sales"]["sale_date"].dt.to_period("M").astype(str)
    ).groupby(DIMS + ["period"], observed=True).ngroups
    assert len(merged["sales_with_ref"]) == n_groups
    assert len(merged["sales_with_ref"]) < len(loaded["sales"])


# ---------------------------------------------------------------------------
# Totals reconcile to the source feeds
# ---------------------------------------------------------------------------
def test_volume_in_reconciles_to_sales(loaded, merged):
    assert merged["sales_with_ref"]["volume_in"].sum() == len(loaded["sales"])


def test_both_conversion_axes_reconcile(loaded, merged):
    n = len(loaded["conversions"])
    conv = merged["conversions_with_ref"]
    assert conv["volume_converted_landed"].sum() == n
    assert conv["volume_converted_cohort"].sum() == n


def test_price_counts_split_priced_and_unpriced(merged):
    conv = merged["conversions_with_ref"]
    # every landed gain is either priced or not
    assert (conv["priced_gains"] + conv["unpriced_gains"]
            == conv["volume_converted_landed"]).all()
    # price_mean is NaN exactly when there were no priced gains
    assert conv.loc[conv["priced_gains"] == 0, "price_mean"].isna().all()
    assert conv.loc[conv["priced_gains"] > 0, "price_mean"].notna().all()


def test_fallout_reconciles(loaded, merged):
    fo = merged["fallout"]
    n_sales, n_conv = len(loaded["sales"]), len(loaded["conversions"])
    assert fo["submissions"].sum() == n_sales
    assert fo["matched"].sum() == n_conv                  # conversions ⊆ sales, unique keys
    assert fo["unmatched"].sum() == n_sales - n_conv
    assert (fo["unmatched"] == fo["submissions"] - fo["matched"]).all()


# ---------------------------------------------------------------------------
# GL resolution: overhead excluded, geography attached, all resolved on clean data
# ---------------------------------------------------------------------------
def test_gl_acquisition_excludes_overhead(merged):
    acq = merged["gl_acquisition"]
    assert (acq["spend_category"] == "acquisition_marketing").all()
    assert "5900" not in set(acq["cost_center"])          # corporate overhead
    # gl_full keeps overhead; the attached mapping columns are present
    assert (merged["gl_full"]["spend_category"] == "overhead").any()
    assert {"entity", "region", "segment", "spend_category"} <= set(merged["gl_full"].columns)


def test_clean_gl_all_resolves(merged):
    assert merged["unmatched_gl"].empty
    # acquisition lines all carry a resolved segment/geography
    acq = merged["gl_acquisition"]
    assert (acq["segment"].str.len() > 0).all()
    assert (acq["entity"].str.len() > 0).all()


# ---------------------------------------------------------------------------
# First-run flag + forecast + string-key join
# ---------------------------------------------------------------------------
def test_unmatched_sales_is_distinct_leaf_period_with_no_plan(loaded, merged):
    um = merged["unmatched_sales"]
    assert list(um.columns) == DIMS + ["period"]
    assert not um.duplicated().any()
    # whatever lands here must genuinely have no plan row
    plan = loaded["reference_data"]
    plan = plan[plan["reference_type"] == "plan"].assign(
        period=lambda d: d["date"].dt.to_period("M").astype(str))
    plan_keys = set(plan[DIMS + ["period"]].itertuples(index=False, name=None))
    for row in um[DIMS + ["period"]].itertuples(index=False, name=None):
        assert row not in plan_keys
    # and the flag on the main frame agrees
    assert (~merged["sales_with_ref"]["has_plan"]).sum() == len(um)


def test_forecast_columns_attached_for_issued_units(merged):
    conv = merged["conversions_with_ref"]
    fc_cols = [c for c in conv.columns if c.endswith("_ref_fc")]
    assert fc_cols, "forecast ref columns should be attached"
    has_fc = conv["volume_converted_ref_fc"].notna()
    assert has_fc.any()                                   # the 6 forecast rows landed
    # forecasts in this snapshot were issued only for ERCOT Door_to_Door / Telemarketing
    assert (conv.loc[has_fc, "entity"] == "ERCOT").all()
    assert set(conv.loc[has_fc, "segment"]) <= {"Door_to_Door", "Telemarketing"}
    assert (conv.loc[has_fc, "period"] == "2024-05").all()


def test_month_to_month_keys_join_on_empty_string(merged):
    swr = merged["sales_with_ref"]
    m2m = swr[swr["product_type"] == "Month_to_Month"]
    assert len(m2m) > 0
    assert (m2m["contract_term_months"] == "").all()      # normalized, not NaN/"nan"
    assert m2m["contract_term_months"].notna().all()
    assert m2m["has_plan"].all()                           # the "" key matched its plan
