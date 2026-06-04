#!/usr/bin/env python3
"""projection_engine.py — period-end volume projections for the current period.

Build Sequence §19 step 3 (context doc §15), after ``metrics_calculator``. It answers the
proactive question (§6) — *"if current trends continue, where will we end the period, and how
does that compare to plan?"* — for the **current, still-accumulating period only**.

Decisions (see ``docs/decisions_log.md`` — BS3 analytics core, module 2):

* **Only the current period projects.** ``metrics_calculator`` already emits
  ``is_projectable = (period == snapshot month)``; this module *consumes* that flag and never
  re-derives it. A settled (or prior, still-settling) period has actuals, not a projection.

* **Volume is the projected metric, at leaf grain.** ``volume_converted`` (activations — the §11
  volume-miss signal) is projected; ``volume_in`` (submissions) rides the same machinery in
  parallel. Two lines, both always produced:
  - **linear** — pace-to-date scaled to the full period (the simple, always-works backup);
  - **weighted** — least-squares slope over the trailing-21-day **cumulative** series, extended to
    period end. Regressing the *cumulative* (not daily increments) keeps the slope robust to bursty
    daily activity. Falls back to all-available days when the period is <21 days old, and to the
    linear line when the fit is degenerate.

* **CPA is NOT projected here.** CPA is ledger-driven (invoice-paced, not daily). Its *current run
  rate* (spend-to-date CPA) and *month-end estimate* (trailing CPA from prior months, or a
  no-history state) already live in the metrics ``cpa`` frame; ``findings_builder`` pairs them.
  This module does no CPA math.

* **Emit values, not variances.** Output is projected values + plan targets (full-period and
  pro-rated to-date). ``risk_classifier`` computes every variance and assigns risk.

* **Daily series is reconstructed in-module** from the raw ``sales`` / ``conversions`` feeds — no
  upstream change.

* **Pro-rating** (``calendar_days`` default / ``business_days``) sets the day-count basis. Activations
  land every day (a weekday-only channel still converts on weekends, ~2 days after the sale), so
  ``calendar_days`` is the right default for the activation projection; ``business_days`` is a
  per-unit seam that matters mainly for submissions of weekday-only channels.

CLI:
    python -m app.analytics.projection_engine
    # load → merge → process_gl → metrics → project the configured snapshot; print the frame.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from app.analytics.data_merger import DIMS
from app.analytics.metrics_calculator import _snapshot_period  # period == snapshot month helper

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

CALENDAR, BUSINESS = "calendar_days", "business_days"

# projection_method labels
M_REG_21D, M_REG_ALL, M_LINEAR = "regression_21d", "regression_all", "linear_fallback"
REGRESSION_WINDOW = 21          # trailing days for the weighted regression
CONF_LOW, CONF_MED, CONF_HIGH = "low", "medium", "high"

# The two volume metrics projected, each (feed, date column, plan-target column).
VOLUME_SPECS = {
    "converted": ("conversions", "conversion_date", "volume_converted_ref"),
    "submissions": ("sales", "sale_date", "volume_in_ref"),
}


# ===========================================================================
# 1. Day-count basis — calendar or business days within the current period
# ===========================================================================
def _day_grid(period: pd.Period, *, upto: dt.date | None, basis: str) -> pd.DatetimeIndex:
    """Contiguous day grid for a month-period in the chosen basis, optionally truncated at
    ``upto`` (inclusive). ``business_days`` drops weekends — the natural basis for weekday-only
    submission channels; ``calendar_days`` keeps every day."""
    end = upto or period.end_time.date()
    grid = pd.date_range(period.start_time.date(), end, freq="D")
    if basis == BUSINESS:
        grid = grid[np.is_busday(grid.values.astype("datetime64[D]"))]
    return grid


def _day_counts(snapshot_date: dt.date, basis: str) -> tuple[int, int, int]:
    """(``days_in_period``, ``days_elapsed``, ``days_remaining``) for the snapshot's month in the
    chosen basis. ``days_elapsed`` counts basis-days from the 1st through the snapshot date."""
    period = pd.Period(snapshot_date, freq="M")
    days_in = len(_day_grid(period, upto=None, basis=basis))
    days_elapsed = len(_day_grid(period, upto=snapshot_date, basis=basis))
    return days_in, days_elapsed, days_in - days_elapsed


# ===========================================================================
# 2. The projection of one leaf's one metric (linear + weighted regression)
# ===========================================================================
def _project_series(daily: pd.Series, *, period: pd.Period, snapshot_date: dt.date, basis: str,
                    days_in: int, days_elapsed: int, days_remaining: int) -> dict:
    """Project one leaf×metric to period end.

    ``daily`` is that leaf's per-day count (sparse) for the current period. We build the cumulative
    series on the contiguous basis-day grid, then:
      * **linear**   = to-date × days_in_period / days_elapsed (pace-to-date scaled up);
      * **weighted** = to-date + slope(trailing-21-day cumulative) × days_remaining.
    Weighted falls back to all-available days (<21 elapsed) and to linear (<2 points)."""
    grid = _day_grid(period, upto=snapshot_date, basis=basis)
    cum = daily.reindex(grid, fill_value=0).cumsum()
    to_date = float(cum.iloc[-1]) if len(cum) else 0.0

    linear = to_date * days_in / days_elapsed if days_elapsed > 0 else np.nan

    n = len(cum)
    if n >= 2:
        window = min(REGRESSION_WINDOW, n)
        y = cum.iloc[-window:].to_numpy(dtype=float)
        x = np.arange(window, dtype=float)
        slope = float(np.polyfit(x, y, 1)[0])               # recent per-basis-day run-rate
        weighted = to_date + slope * days_remaining
        method = M_REG_21D if window >= REGRESSION_WINDOW else M_REG_ALL
    else:
        weighted, method = linear, M_LINEAR                 # not enough points to fit a line

    return {"to_date": to_date, "proj_linear": linear,
            "proj_weighted": weighted, "proj_method": method}


def _confidence(days_elapsed: int, days_in: int) -> str:
    """Confidence bucket from the share of the period elapsed (§6 — always shown, never hidden)."""
    frac = days_elapsed / days_in if days_in else 0.0
    return CONF_LOW if frac < 1 / 3 else CONF_MED if frac < 2 / 3 else CONF_HIGH


# ===========================================================================
# 3. Core — project every projectable leaf, both volume metrics
# ===========================================================================
def project_volume(feeds: dict[str, pd.DataFrame], merged: dict[str, pd.DataFrame],
                   metrics: dict[str, pd.DataFrame], *, snapshot_date: dt.date,
                   pro_rate: str = CALENDAR) -> dict[str, pd.DataFrame]:
    """Project period-end volume for the current period, at leaf grain.

    Returns ``{"volume_projection": df}`` with, per leaf×current-period: day-count context +
    confidence, and for each metric (``converted_*`` / ``submissions_*``) the to-date value, the
    two projection lines, the method label, and the plan targets (full + pro-rated)."""
    basis = pro_rate if pro_rate in (CALENDAR, BUSINESS) else CALENDAR

    # Current period = the one metrics flags projectable (consumed, not re-derived).
    econ = metrics["economics"]
    proj_periods = econ.loc[econ["is_projectable"], "period"].unique()
    if len(proj_periods) == 0:
        logger.info("projection: no projectable (current) period in this snapshot — empty frame")
        return {"volume_projection": _empty_projection_frame()}
    current_period = str(proj_periods[0])
    period = pd.Period(current_period, freq="M")
    days_in, days_elapsed, days_remaining = _day_counts(snapshot_date, basis)
    confidence = _confidence(days_elapsed, days_in)
    logger.info("projection: period %s, basis %s, %d/%d days elapsed (%s confidence)",
                current_period, basis, days_elapsed, days_in, confidence)

    # Project each metric independently, then outer-merge on the leaf identity so a leaf with only
    # submissions (or only conversions) this period still appears.
    per_metric = {name: _project_metric(name, feeds, merged, current_period, period,
                                        snapshot_date=snapshot_date, basis=basis, days_in=days_in,
                                        days_elapsed=days_elapsed, days_remaining=days_remaining)
                  for name in VOLUME_SPECS}

    out = None
    for df in per_metric.values():
        out = df if out is None else out.merge(df, on=DIMS, how="outer")

    # Day-count context is the same for every leaf in this period.
    out.insert(len(DIMS), "period", current_period)
    out["days_basis"] = basis
    out["days_elapsed"] = days_elapsed
    out["days_in_period"] = days_in
    out["confidence"] = confidence
    logger.info("projection: %d leaf row(s)", len(out))
    return {"volume_projection": out.reset_index(drop=True)}


def _project_metric(name: str, feeds: dict, merged: dict, current_period: str, period: pd.Period,
                    *, snapshot_date: dt.date, basis: str, days_in: int, days_elapsed: int,
                    days_remaining: int) -> pd.DataFrame:
    """Project one volume metric (``converted`` or ``submissions``) for every leaf active this
    period, and attach its plan targets (full-period + pro-rated)."""
    feed_name, date_col, plan_col = VOLUME_SPECS[name]
    feed = feeds[feed_name]
    cur = feed[feed[date_col].dt.to_period("M").astype(str) == current_period].copy()
    cur["_day"] = cur[date_col].dt.normalize()

    rows = []
    for leaf_vals, g in cur.groupby(DIMS, observed=True):
        daily = g.groupby("_day").size()
        proj = _project_series(daily, period=period, snapshot_date=snapshot_date, basis=basis,
                               days_in=days_in, days_elapsed=days_elapsed,
                               days_remaining=days_remaining)
        leaf_vals = leaf_vals if isinstance(leaf_vals, tuple) else (leaf_vals,)
        rows.append({**dict(zip(DIMS, leaf_vals)),
                     f"{name}_to_date": proj["to_date"],
                     f"{name}_proj_linear": proj["proj_linear"],
                     f"{name}_proj_weighted": proj["proj_weighted"],
                     f"{name}_proj_method": proj["proj_method"]})
    df = pd.DataFrame(rows, columns=DIMS + [f"{name}_to_date", f"{name}_proj_linear",
                                            f"{name}_proj_weighted", f"{name}_proj_method"])

    # Plan target for this leaf×period (full-period), pro-rated to elapsed share.
    ref_frame = merged["conversions_with_ref" if name == "converted" else "sales_with_ref"]
    plan = ref_frame.loc[ref_frame["period"] == current_period, DIMS + [plan_col]].copy()
    plan = plan.rename(columns={plan_col: f"{name}_plan_full"})
    df = df.merge(plan, on=DIMS, how="left")
    df[f"{name}_plan_prorated"] = df[f"{name}_plan_full"] * days_elapsed / days_in
    return df


def _empty_projection_frame() -> pd.DataFrame:
    """Schema-stable empty frame for the no-current-period case (never a bare/None return)."""
    cols = DIMS + ["period", "days_basis", "days_elapsed", "days_in_period", "confidence"]
    for name in VOLUME_SPECS:
        cols += [f"{name}_to_date", f"{name}_proj_linear", f"{name}_proj_weighted",
                 f"{name}_proj_method", f"{name}_plan_full", f"{name}_plan_prorated"]
    return pd.DataFrame(columns=cols)


# ===========================================================================
# 4. Thin wrapper — load → merge → gl → metrics → project
# ===========================================================================
def compute_projections(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict[str, pd.DataFrame]:
    """Convenience entry: run the upstream chain and project, reading ``snapshot_date`` +
    ``pro_rate_default`` from system_config (mirrors ``metrics_calculator.compute_metrics``)."""
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames
    from app.analytics.gl_processor import process_gl
    from app.analytics.metrics_calculator import calculate_metrics

    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    period_close_day = int(cfg["period_close_day"])
    pro_rate = str(cfg.get("pro_rate_default", CALENDAR))

    data = load_data(config_path)
    merged = merge_frames(data)
    gl_states = process_gl(merged["gl_acquisition"], config_path)
    metrics = calculate_metrics(merged, gl_states, data["cogs_config"], data["retention_config"],
                                snapshot_date=snapshot_date, period_close_day=period_close_day)
    return project_volume(data, merged, metrics, snapshot_date=snapshot_date, pro_rate=pro_rate)


# ===========================================================================
# 5. CLI — manual verification aid
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    vp = compute_projections()["volume_projection"]
    print(f"\n=== volume_projection  ({vp.shape[0]:,} × {vp.shape[1]}) ===")
    if len(vp):
        for col in [c for c in vp.columns if c.endswith("_proj_method")]:
            print(f"  {col}: {vp[col].value_counts().to_dict()}")
        print(f"  confidence: {vp['confidence'].value_counts().to_dict()} | "
              f"days {vp['days_elapsed'].iloc[0]}/{vp['days_in_period'].iloc[0]} "
              f"({vp['days_basis'].iloc[0]})")
        show = (DIMS[:1] + ["region", "segment", "converted_to_date", "converted_proj_linear",
                "converted_proj_weighted", "converted_proj_method", "converted_plan_full"])
        print(vp.sort_values("converted_to_date", ascending=False)[show].head(8).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
