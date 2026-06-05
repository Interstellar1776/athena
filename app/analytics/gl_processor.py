#!/usr/bin/env python3
"""gl_processor.py — GL completeness state + late-invoice detection.

Build Sequence §19 step 3 (context doc §15), after ``data_merger``. It takes the resolved
acquisition ledger (``gl_acquisition``) and tells the metrics layer, per
``(entity, region, segment, period)``, whether that period's spend is authoritative or
still partial — by reading the **posting-vs-document date gap**, no accrual flags required
(§10, §13).

Core model (decided with the project owner — see the plan / commit history):

* **Spend ties to the month it belongs to** → ``period`` is the **document_date** month. A
  late invoice counts in the month it documents to, not when it posted.
* **Close date = following month, day ``period_close_day`` (8).** May closes June 8 (matches
  the system_config comment and the post-close June-8 snapshot). A period is *past close*
  when ``snapshot_date >= close(period)``.
* **Completeness state** per ``(unit, period P)``, ``current = month(snapshot_date)``,
  first match wins:
    1. ``open``     — ``P == current`` or ``P`` not yet past close (still settling).
    2. ``accrued``  — past close AND an entry **posted in the current period** (prior-month
                      spend landing now). Checked before ``restated`` so the late-April
                      invoice reads as ``accrued`` (§10), which the literal order would miss.
    3. ``restated`` — past close AND an entry **posted after** ``close(P)`` (a settled month
                      changed).
    4. ``closed``   — past close, none of the above.

* **Late invoices** (posting month ≠ document month) are flagged in dedicated columns,
  attributed to the document month — detection is independent of the state label, so both
  engineered rows (the late-April invoice and the post-close May true-up) are always caught.

CLI:
    python -m app.analytics.gl_processor
    # load → merge → process the configured snapshot; print the state distribution.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# Repo-relative config path (mirrors data_loader's location-stable convention).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

UNIT = ["entity", "region", "segment"]            # GL resolves to this (unit) grain
OPEN, CLOSED, RESTATED, ACCRUED = "open", "closed", "restated", "accrued"


# ===========================================================================
# 1. Period / close-date helpers
# ===========================================================================
def _close_date(period: pd.Period, period_close_day: int) -> dt.date:
    """Close date of a month-period: day ``period_close_day`` of the *following* month."""
    following = period + 1                        # next month-period
    return dt.date(following.year, following.month, period_close_day)


def _state_for_group(doc_period: pd.Period, posting_periods: pd.Series,
                     current_period: pd.Period, snapshot_date: dt.date,
                     period_close_day: int) -> str:
    """Completeness state for one (unit, document-period) group.

    ``posting_periods`` is the month each entry in the group *posted* in — the gap between
    those and ``doc_period`` is what distinguishes the four states."""
    close = _close_date(doc_period, period_close_day)
    past_close = snapshot_date >= close

    # 1. open — the current month, or a prior month still inside its settlement window.
    if doc_period == current_period or not past_close:
        return OPEN
    # 2. accrued — a prior-month cost that posted in the current period (landing now).
    if (posting_periods == current_period).any():
        return ACCRUED
    # 3. restated — a settled month that received an entry after its close date.
    if (posting_periods > doc_period).any():
        return RESTATED
    # 4. closed — past close, every entry posted within the month.
    return CLOSED


# ===========================================================================
# 2. Core — completeness state + late-invoice flags per (unit, period)
# ===========================================================================
def gl_completeness(gl_acquisition: pd.DataFrame, *, snapshot_date: dt.date,
                    period_close_day: int) -> pd.DataFrame:
    """Assign a completeness state and late-invoice flags per (unit, period).

    Returns one row per ``(entity, region, segment, period)`` with columns:
    ``gl_completeness_state``, ``total_spend``, ``late_invoice_amount``, ``late_invoice_count``.
    ``period`` is the document-date month (``YYYY-MM``); spend ties to the month it belongs to.
    """
    gl = gl_acquisition.copy()

    # Period axes: document month (where spend belongs) and posting month (when it landed).
    gl["period"] = gl["document_date"].dt.to_period("M")
    gl["_posting_period"] = gl["posting_date"].dt.to_period("M")
    gl["_is_late"] = gl["_posting_period"] != gl["period"]
    gl["_late_amount"] = gl["amount"].where(gl["_is_late"], 0.0)

    current_period = pd.Period(snapshot_date, freq="M")

    # Aggregate spend + late-invoice flags per (unit, document-period).
    grouped = gl.groupby(UNIT + ["period"], observed=True)
    out = grouped.agg(
        total_spend=("amount", "sum"),
        late_invoice_amount=("_late_amount", "sum"),
        late_invoice_count=("_is_late", "sum"),
    ).reset_index()
    out["late_invoice_count"] = out["late_invoice_count"].astype("int64")

    # Completeness state needs each group's posting months, so resolve per group.
    states = {
        keys: _state_for_group(keys[-1], g["_posting_period"], current_period,
                               snapshot_date, period_close_day)
        for keys, g in grouped
    }
    out["gl_completeness_state"] = [
        states[tuple(row)] for row in out[UNIT + ["period"]].itertuples(index=False, name=None)
    ]

    # period as YYYY-MM string for a clean, joinable key downstream.
    out["period"] = out["period"].astype(str)
    out = out[UNIT + ["period", "gl_completeness_state",
                      "total_spend", "late_invoice_amount", "late_invoice_count"]]

    n_late = int(out["late_invoice_count"].sum())
    logger.info("gl_processor: %d (unit, period) rows; states=%s; %d late invoice line(s) "
                "totaling %.2f", len(out),
                out["gl_completeness_state"].value_counts().to_dict(),
                n_late, out["late_invoice_amount"].sum())
    return out


# ===========================================================================
# 3. Thin wrapper — pull snapshot_date + period_close_day from system_config
# ===========================================================================
def process_gl(gl_acquisition: pd.DataFrame,
               config_path: Path = DEFAULT_SYSTEM_CONFIG) -> pd.DataFrame:
    """Convenience entry: read ``snapshot_date`` + ``period_close_day`` from system_config,
    then run :func:`gl_completeness`."""
    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    return gl_completeness(gl_acquisition, snapshot_date=snapshot_date,
                           period_close_day=int(cfg["period_close_day"]))


# ===========================================================================
# 4. CLI — manual verification aid
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames

    gl_states = process_gl(merge_frames(load_data())["gl_acquisition"])
    print("GL completeness by state:")
    print(gl_states["gl_completeness_state"].value_counts().to_string())
    print("\nLate invoices:")
    late = gl_states[gl_states["late_invoice_count"] > 0]
    print(late.to_string(index=False) if len(late) else "  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
