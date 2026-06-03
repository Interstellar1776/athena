"""Tests for the GL processor (Build Sequence §19 step 3).

Two layers:
  1. synthetic rows that pin each completeness state and the close-date boundary
     deterministically (no dependence on the big snapshot);
  2. integration on the real snapshots — the engineered late-April invoice (May-22) and
     post-close May true-up (June-8) must be detected, and spend must reconcile.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.data_loader import load_data                       # noqa: E402
from app.analytics.data_merger import merge_frames                    # noqa: E402
from app.analytics.gl_processor import gl_completeness, process_gl    # noqa: E402

CLOSE_DAY = 8
REAL_SNAPSHOTS = REPO_ROOT / "data" / "snapshots"
REAL_CONFIG = REPO_ROOT / "config"


def _row(doc: str, post: str, amount: float = 100.0,
         entity="ERCOT", region="North", segment="Web_Direct") -> dict:
    return {"entity": entity, "region": region, "segment": segment,
            "document_date": pd.Timestamp(doc), "posting_date": pd.Timestamp(post),
            "amount": amount}


def _states(rows, snapshot_date: str):
    df = gl_completeness(pd.DataFrame(rows), snapshot_date=dt.date.fromisoformat(snapshot_date),
                         period_close_day=CLOSE_DAY)
    return df.set_index("period")["gl_completeness_state"].to_dict()


# ---------------------------------------------------------------------------
# 1. Synthetic — one state per case
# ---------------------------------------------------------------------------
def test_open_is_current_period():
    # document month == snapshot month → open
    assert _states([_row("2024-05-03", "2024-05-03")], "2024-05-22") == {"2024-05": "open"}


def test_open_prior_month_still_in_settlement_window():
    # May not yet past close (June 8) at a June-05 snapshot → still open/settling
    assert _states([_row("2024-05-30", "2024-05-30")], "2024-06-05") == {"2024-05": "open"}


def test_closed_past_close_all_in_month():
    assert _states([_row("2024-03-10", "2024-03-10")], "2024-05-22") == {"2024-03": "closed"}


def test_accrued_prior_doc_posts_in_current_period():
    # doc April, posts May (current at the May-22 snapshot) → accrued
    assert _states([_row("2024-04-27", "2024-05-20")], "2024-05-22") == {"2024-04": "accrued"}


def test_restated_entry_posted_after_close_but_not_current():
    # doc March (closes Apr 8), entry posts Apr 20 (> close, not current May) → restated
    assert _states([_row("2024-03-15", "2024-04-20")], "2024-05-22") == {"2024-03": "restated"}


def test_accrued_checked_before_restated():
    # A row that posts after close AND in the current period resolves to accrued (not restated).
    assert _states([_row("2024-04-27", "2024-05-20")], "2024-05-22")["2024-04"] == "accrued"


def test_spend_ties_to_document_month_and_aggregates():
    rows = [_row("2024-04-27", "2024-05-20", amount=9800.0),   # late, documents to April
            _row("2024-04-02", "2024-04-02", amount=200.0)]    # normal April
    df = gl_completeness(pd.DataFrame(rows), snapshot_date=dt.date(2024, 5, 22),
                         period_close_day=CLOSE_DAY)
    apr = df[df["period"] == "2024-04"].iloc[0]
    assert apr["total_spend"] == 10000.0           # both tie to April
    assert apr["late_invoice_amount"] == 9800.0    # only the cross-month one
    assert apr["late_invoice_count"] == 1


# ---------------------------------------------------------------------------
# 2. Integration — real snapshots
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def gl_states_may22():
    return process_gl(merge_frames(load_data())["gl_acquisition"])


def test_one_row_per_unit_period(gl_states_may22):
    keys = ["entity", "region", "segment", "period"]
    assert not gl_states_may22.duplicated(keys).any()


def test_total_spend_reconciles_to_acquisition(gl_states_may22):
    acq = merge_frames(load_data())["gl_acquisition"]
    assert gl_states_may22["total_spend"].sum() == pytest.approx(acq["amount"].sum())


def test_current_period_open_old_months_closed(gl_states_may22):
    assert (gl_states_may22.loc[gl_states_may22["period"] == "2024-05",
                                "gl_completeness_state"] == "open").all()
    assert (gl_states_may22.loc[gl_states_may22["period"] == "2023-08",
                                "gl_completeness_state"] == "closed").all()


def test_late_april_invoice_detected_may22(gl_states_may22):
    apr = gl_states_may22[(gl_states_may22["period"] == "2024-04")
                          & (gl_states_may22["late_invoice_count"] > 0)]
    assert len(apr) == 1
    row = apr.iloc[0]
    assert row["segment"] == "Door_to_Door" and row["entity"] == "ERCOT"
    assert row["late_invoice_amount"] == 9800.0
    assert row["gl_completeness_state"] == "accrued"


def _load_snapshot(tmp_path: Path, snapshot_date: str):
    """Build a temp system_config pointing at the real snapshots for a given date."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for t in ("gl_mapping", "cogs_config", "retention_config"):
        shutil.copy(REAL_CONFIG / f"{t}.csv", cfg_dir / f"{t}.csv")
    cfg = {"data_mode": "snapshot", "snapshot_date": snapshot_date, "period_close_day": CLOSE_DAY,
           "snapshot_path": str(REAL_SNAPSHOTS) + "/", "live_data_path": str(tmp_path) + "/"}
    cfg_path = cfg_dir / "system_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    return cfg_path


def test_may_trueup_detected_at_postclose_snapshot(tmp_path):
    cfg_path = _load_snapshot(tmp_path, "2024-06-08")
    merged = merge_frames(load_data(cfg_path))
    gl_states = process_gl(merged["gl_acquisition"], config_path=cfg_path)
    may = gl_states[(gl_states["period"] == "2024-05")
                    & (gl_states["late_invoice_count"] > 0)]
    assert len(may) == 1
    assert may.iloc[0]["late_invoice_amount"] == 4000.0
    # both engineered late invoices are present in the post-close cut
    assert gl_states["late_invoice_count"].sum() == 2
