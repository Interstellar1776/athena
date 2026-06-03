#!/usr/bin/env python3
"""metrics_calculator.py — the four metrics + fallout, each labeled with its method.

Build Sequence §19 step 3 (context doc §15), after ``gl_processor``. This is the layer that
turns the joined/aggregated frames into business metrics — **COGS, margin, LTV, CPA, and
fallout** — applying the §9–§10 fallback hierarchies and **labeling every output with the
method that produced it**. It computes *truth*; it does **not** classify risk, decide
thresholds, or project to period-end (those are ``risk_classifier`` / ``projection_engine``).

Decisions baked in (see ``docs/decisions_log.md`` — BS3 planning + analytics-core):

* **Metric-driven grain.** CPA is **unit grain** ``(entity, region, segment)`` — the grain GL
  resolves to; the ledger has no product/customer dimensions, so a leaf CPA would fabricate
  precision. COGS / margin / LTV / fallout are **leaf grain** (the full 8-dim hierarchy).
  Output is therefore three frames, each at its native grain — never collapsed.

* **Compute order COGS → margin → LTV → CPA → fallout.** Margin needs COGS; LTV
  (``calculated_retention``) needs margin. Running in dependency order means each metric's
  inputs already exist when it computes.

* **Open-period CPA is period-to-date.** For an ``open`` period we report
  spend-to-date ÷ conversions-to-date and mark ``is_projectable=True``; **all** full-period
  scaling lives in ``projection_engine`` (one projector, no double-extrapolation).

* **COGS is time-varying.** The current/actual COGS is the ``cogs_config`` rate *effective as
  of the period* (effective-dated; updated monthly going forward). This module **exposes** the
  comparison inputs (current / plan / forecast / trailing-3 / prior-year) and the per-leaf
  ``cogs_comparison_mode``; ``risk_classifier`` evaluates the mode and computes the delta.

* **Severity ≠ confidence.** This module emits a per-metric ``*_estimated`` boolean (true for
  any non-``real`` / non-``calculated`` method). ``risk_classifier`` sets magnitude-only
  severity; the two never interact (an estimated HIGH stays HIGH).

* **Trailing paths need ≥3 months of history** or fall back to plan, labeled accordingly.

CLI:
    python -m app.analytics.metrics_calculator
    # load → merge → process_gl → calculate; print each frame + every method-label distribution.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from app.analytics.data_merger import DIMS                 # 8-field leaf identity
from app.analytics.gl_processor import UNIT, process_gl    # ["entity","region","segment"]

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

# ---------------------------------------------------------------------------
# Method-label vocabularies (mirror context doc §10/§14). Kept as constants so a
# typo can't silently introduce a new label downstream.
# ---------------------------------------------------------------------------
# CPA actual_method
CPA_REAL, CPA_GL_PARTIAL, CPA_TRAILING, CPA_PLAN = (
    "real", "gl_partial", "trailing_avg", "plan_input")
# COGS method
COGS_ACTUAL, COGS_TRAILING, COGS_PLAN, COGS_ESTIMATED = (
    "actual", "trailing_avg", "plan_input", "estimated")
# margin method
MARGIN_CALC, MARGIN_PLAN = "calculated", "plan_input"
# LTV method
LTV_RETENTION, LTV_TERM, LTV_PLAN, LTV_UNRESOLVED = (
    "calculated_retention", "calculated_term", "plan_input", "unresolved")
FALLOUT_REAL = "real"

# GL states under which the period's spend is authoritative (a real recompute), vs. `open`
# which is still in-progress (period-to-date → gl_partial).
AUTHORITATIVE_GL_STATES = {"closed", "restated", "accrued"}

TRAILING_MONTHS = 3            # window for trailing-average fallbacks + LTV retention avg
TRAILING_LONG_MONTHS = 12      # T12M CPA window
MIN_HISTORY_MONTHS = 3         # ≥3 months or fall back to plan (§9)
# Conversion-lag SLA (decisions_log: period_close_day 8 ≥ this). A cohort is fully resolved
# once the snapshot is this many days past the cohort month-end; until then it is pending.
CONV_LAG_SLA_DAYS = 7


# ===========================================================================
# 1. Period helpers — month math on the YYYY-MM string keys used everywhere
# ===========================================================================
def _period_end(period: str) -> dt.date:
    """Last calendar day of a ``YYYY-MM`` period."""
    p = pd.Period(period, freq="M")
    return p.to_timestamp(how="end").date()


def _snapshot_period(snapshot_date: dt.date) -> str:
    """The ``YYYY-MM`` the snapshot falls in (the 'current' month for open/projectable)."""
    return pd.Period(snapshot_date, freq="M").strftime("%Y-%m")


def _rolling_by_key(df: pd.DataFrame, keys: list[str], value_cols: list[str], window: int,
                    *, how: str = "sum", min_periods: int = 1, shift: int = 0,
                    suffix: str = "") -> pd.DataFrame:
    """Trailing window over a *contiguous* monthly grid, per key group.

    Each key group is reindexed to a gap-free monthly range (so a missing month counts as a
    real gap, not a silent skip) before the rolling op. ``shift=1`` excludes the current month
    (a strictly-*prior* trailing window, used for the no-spend CPA fallback). Returns the
    original ``keys + ['period']`` with the rolled ``value_cols`` (renamed with ``suffix``).
    """
    parts = []
    for kvals, g in df.groupby(keys, observed=True, sort=False):
        g = g.copy()
        g["_p"] = g["period"].map(lambda s: pd.Period(s, freq="M"))
        g = g.set_index("_p").sort_index()
        full = pd.period_range(g.index.min(), g.index.max(), freq="M")
        g = g.reindex(full)                                  # gap-free monthly grid
        roll = g[value_cols].shift(shift).rolling(window, min_periods=min_periods)
        rolled = roll.sum() if how == "sum" else roll.mean()
        rolled = rolled.add_suffix(suffix)
        rolled["period"] = [p.strftime("%Y-%m") for p in full]
        kvals = kvals if isinstance(kvals, tuple) else (kvals,)
        for col, val in zip(keys, kvals):
            rolled[col] = val
        parts.append(rolled.reset_index(drop=True))
    out = pd.concat(parts, ignore_index=True)
    return out[keys + ["period"] + [c + suffix for c in value_cols]]


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Element-wise divide that yields NaN (never inf) when the denominator is 0 — so a
    zero-conversion period falls through to a fallback rather than emitting a bogus ratio."""
    denom = denominator.where(denominator != 0, other=np.nan)
    return numerator / denom


# ===========================================================================
# 2. COGS — leaf grain, time-varying (effective-dated)
# ===========================================================================
def _resolve_effective_cogs(leaf_periods: pd.DataFrame,
                            cogs_config: pd.DataFrame) -> pd.DataFrame:
    """Current/actual COGS per leaf×period = the ``cogs_config`` rate whose ``effective_date``
    is the latest on/before the period-end. Built to handle a future *history* of monthly
    rate rows; with today's one-row-per-leaf config a period earlier than the leaf's first
    effective date resolves to NaN (→ falls back to plan downstream).

    Also attaches the per-leaf ``cogs_comparison_mode`` (constant across periods).
    """
    cfg = cogs_config.copy()
    cfg["_eff"] = pd.to_datetime(cfg["effective_date"], errors="raise")

    base = leaf_periods.copy()
    base["_pend"] = pd.to_datetime(base["period"].map(_period_end))

    # Cross each leaf×period with that leaf's config rows, keep rows effective by period-end,
    # then take the latest-effective rate per leaf×period.
    merged = base.merge(cfg[DIMS + ["_eff", "cogs_per_unit", "cogs_comparison_mode"]],
                        on=DIMS, how="left")
    eligible = merged[merged["_eff"] <= merged["_pend"]]
    idx = eligible.groupby(DIMS + ["period"], observed=True)["_eff"].idxmax()
    latest = eligible.loc[idx, DIMS + ["period", "cogs_per_unit", "cogs_comparison_mode"]]
    latest = latest.rename(columns={"cogs_per_unit": "cogs_actual"})

    # comparison_mode is leaf-constant — keep it even for periods with no effective rate yet.
    mode = cfg.drop_duplicates(DIMS)[DIMS + ["cogs_comparison_mode"]]
    out = base[DIMS + ["period"]].merge(
        latest.drop(columns="cogs_comparison_mode"), on=DIMS + ["period"], how="left")
    out = out.merge(mode, on=DIMS, how="left")
    return out


def _cogs(economics: pd.DataFrame, cogs_config: pd.DataFrame) -> pd.DataFrame:
    """Resolve current COGS + the §10 fallback chain, and surface the comparison inputs.

    Fallback (per leaf×period): current effective rate (``actual``) → trailing-3-month avg of
    resolved COGS (``trailing_avg``) → plan ``cogs_ref`` (``plan_input``) → ``estimated``.
    """
    leaf_periods = economics[DIMS + ["period"]].drop_duplicates()
    eff = _resolve_effective_cogs(leaf_periods, cogs_config)
    df = economics.merge(eff, on=DIMS + ["period"], how="left")

    # Trailing-3-month average of the resolved current COGS (prior history; needs ≥3 months).
    trailing = _rolling_by_key(eff[DIMS + ["period", "cogs_actual"]], DIMS, ["cogs_actual"],
                               TRAILING_MONTHS, how="mean", min_periods=MIN_HISTORY_MONTHS,
                               suffix="_t3")
    df = df.merge(trailing.rename(columns={"cogs_actual_t3": "cogs_trailing3"}),
                  on=DIMS + ["period"], how="left")

    # Prior-year same-period resolved COGS (period − 12 months), for prior_year comparison mode.
    py = eff[DIMS + ["period", "cogs_actual"]].copy()
    py["period"] = py["period"].map(lambda s: (pd.Period(s, "M") + 12).strftime("%Y-%m"))
    df = df.merge(py.rename(columns={"cogs_actual": "cogs_prior_year"}),
                  on=DIMS + ["period"], how="left")

    # Plan / forecast COGS come straight off the merged reference columns.
    df["cogs_plan"] = df["cogs_ref"]
    df["cogs_forecast"] = df["cogs_ref_fc"]

    # Resolve the fallback chain into the single COGS the downstream metrics use + its label.
    actual, trail, plan = df["cogs_actual"], df["cogs_trailing3"], df["cogs_plan"]
    df["cogs_per_unit"] = actual.where(actual.notna(),
                          trail.where(trail.notna(), plan))
    df["cogs_method"] = np.select(
        [actual.notna(), trail.notna(), plan.notna()],
        [COGS_ACTUAL, COGS_TRAILING, COGS_PLAN],
        default=COGS_ESTIMATED)
    return df


# ===========================================================================
# 3. Margin — leaf grain (price from conversions − resolved COGS)
# ===========================================================================
def _margin(df: pd.DataFrame) -> pd.DataFrame:
    """``margin_per_unit = price_per_unit − cogs_per_unit`` when price is known, else fall
    back to plan ``margin_ref``. ``margin_per_period = margin_per_unit × landed conversions``."""
    df["price_per_unit"] = df["price_mean"]                 # mean of priced gains (leaf×period)
    calc = df["price_per_unit"] - df["cogs_per_unit"]
    has_price = df["price_per_unit"].notna() & df["cogs_per_unit"].notna()

    df["margin_per_unit"] = calc.where(has_price, df["margin_ref"])
    df["margin_method"] = np.where(has_price, MARGIN_CALC, MARGIN_PLAN)
    df["margin_per_period"] = df["margin_per_unit"] * df["volume_converted_landed"]
    return df


# ===========================================================================
# 4. LTV — leaf grain, calculate-first hierarchy
# ===========================================================================
def _ltv(df: pd.DataFrame, retention_config: pd.DataFrame) -> pd.DataFrame:
    """LTV hierarchy (§10): ``calculated_retention`` → ``calculated_term`` → ``plan_input``
    → ``unresolved``. Retention is per-sub-segment config; ``calculated_term`` only serves
    Term leaves that lack a retention input."""
    ret = retention_config[DIMS + ["expected_retention_periods"]]
    df = df.merge(ret, on=DIMS, how="left")
    has_retention = df["expected_retention_periods"].notna()

    # Trailing-3-month average of monthly margin_per_unit (needs ≥3 months of margin history).
    avg_margin = _rolling_by_key(df[DIMS + ["period", "margin_per_unit"]], DIMS,
                                 ["margin_per_unit"], TRAILING_MONTHS, how="mean",
                                 min_periods=MIN_HISTORY_MONTHS, suffix="_avg3")
    df = df.merge(avg_margin.rename(columns={"margin_per_unit_avg3": "_margin_avg3"}),
                  on=DIMS + ["period"], how="left")

    ltv_retention = df["_margin_avg3"] * df["expected_retention_periods"]
    # calculated_term: Term only (contract_term_months present) AND retention unconfigured.
    is_term = df["contract_term_months"].astype(str).str.len() > 0
    ltv_term = df["margin_per_unit"] * pd.to_numeric(df["contract_term_months"], errors="coerce")

    use_retention = has_retention & ltv_retention.notna()
    use_term = ~has_retention & is_term & ltv_term.notna()
    use_plan = ~use_retention & ~use_term & df["ltv_ref"].notna()

    df["ltv"] = np.select(
        [use_retention, use_term, use_plan],
        [ltv_retention, ltv_term, df["ltv_ref"]], default=np.nan)
    df["ltv_method"] = np.select(
        [use_retention, use_term, use_plan],
        [LTV_RETENTION, LTV_TERM, LTV_PLAN], default=LTV_UNRESOLVED)
    return df.drop(columns=["_margin_avg3"])


# ===========================================================================
# 5. CPA — unit grain (GL spend ÷ landed conversions), with estimation hierarchy
# ===========================================================================
def _unit_conversions(conversions: pd.DataFrame) -> pd.DataFrame:
    """Roll leaf landed conversions up to the unit grain, per period (the CPA denominator)."""
    return (conversions.groupby(UNIT + ["period"], observed=True)
            .agg(conversions_landed=("volume_converted_landed", "sum"))
            .reset_index())


def _unit_plan_cpa(conversions: pd.DataFrame) -> pd.DataFrame:
    """Unit plan CPA per unit×period. ``cpa_ref`` is a unit value repeated on each leaf, so we
    de-duplicate it; cross-check against Σcost_ref ÷ Σvolume_converted_ref and warn on a
    mismatch (which would signal a future ``plan_bias`` the dedup would miss)."""
    g = conversions.groupby(UNIT + ["period"], observed=True)
    out = g.agg(cpa_ref=("cpa_ref", "mean"),
                _cost_ref=("cost_ref", "sum"),
                _convref=("volume_converted_ref", "sum")).reset_index()
    derived = _safe_divide(out["_cost_ref"], out["_convref"])
    mism = (out["cpa_ref"] - derived).abs() > 0.01 * out["cpa_ref"].abs()
    if mism.any():
        logger.warning("cpa_ref disagrees with Σcost_ref/Σconv_ref for %d unit×period row(s) "
                       "— possible plan_bias; using de-duplicated cpa_ref", int(mism.sum()))
    return out[UNIT + ["period", "cpa_ref"]]


def _cpa(conversions: pd.DataFrame, gl_states: pd.DataFrame,
         *, snapshot_period: str) -> pd.DataFrame:
    """Monthly / T3M / T12M CPA at unit grain, with the §10 estimation hierarchy and labels."""
    units = _unit_conversions(conversions)
    plan = _unit_plan_cpa(conversions)

    # Spine = every unit×period that has GL spend OR conversions (outer union).
    df = gl_states[UNIT + ["period", "gl_completeness_state", "total_spend"]].merge(
        units, on=UNIT + ["period"], how="outer").merge(
        plan, on=UNIT + ["period"], how="left")
    df["total_spend"] = df["total_spend"].fillna(0.0)
    df["conversions_landed"] = df["conversions_landed"].fillna(0).astype("int64")

    # Trailing windows (aggregate ratio Σspend ÷ Σconv), informational, computed where any
    # history exists. Inclusive of the current month.
    for win, label in ((TRAILING_MONTHS, "t3"), (TRAILING_LONG_MONTHS, "t12")):
        roll = _rolling_by_key(df[UNIT + ["period", "total_spend", "conversions_landed"]],
                               UNIT, ["total_spend", "conversions_landed"], win,
                               how="sum", min_periods=1, suffix=f"_{label}")
        df = df.merge(roll, on=UNIT + ["period"], how="left")
        df[f"cpa_{label}"] = _safe_divide(df[f"total_spend_{label}"],
                                          df[f"conversions_landed_{label}"])

    # Strictly-prior trailing-3 (shift=1, ≥3 months) — the no-spend fallback basis.
    prior = _rolling_by_key(df[UNIT + ["period", "total_spend", "conversions_landed"]],
                            UNIT, ["total_spend", "conversions_landed"], TRAILING_MONTHS,
                            how="sum", min_periods=MIN_HISTORY_MONTHS, shift=1, suffix="_prior3")
    df = df.merge(prior, on=UNIT + ["period"], how="left")
    cpa_prior3 = _safe_divide(df["total_spend_prior3"], df["conversions_landed_prior3"])

    # --- Estimation hierarchy → cpa_monthly + actual_method ---
    has_spend = (df["total_spend"] > 0) & (df["conversions_landed"] > 0)
    direct = _safe_divide(df["total_spend"], df["conversions_landed"])
    authoritative = df["gl_completeness_state"].isin(AUTHORITATIVE_GL_STATES)

    df["cpa_monthly"] = np.select(
        [has_spend, cpa_prior3.notna(), df["cpa_ref"].notna()],
        [direct, cpa_prior3, df["cpa_ref"]], default=np.nan)
    df["cpa_monthly_method"] = np.select(
        [has_spend & authoritative, has_spend & ~authoritative,
         cpa_prior3.notna(), df["cpa_ref"].notna()],
        [CPA_REAL, CPA_GL_PARTIAL, CPA_TRAILING, CPA_PLAN], default=LTV_UNRESOLVED)

    # is_projectable is a *calendar* fact: only the current period is still accumulating toward
    # its end, so only it can be projected. This is deliberately NOT `gl_completeness_state ==
    # "open"` — gl_processor also marks a *prior* month still inside its settlement grace as
    # `open` (e.g. April at a May-1 snapshot), but April is over and must not be projected; and
    # a current-month unit with no GL posted yet (state NaN) is still projectable. Same rule as
    # the leaf economics frame, so unit and leaf agree.
    df["is_projectable"] = df["period"] == snapshot_period

    df["cpa_estimated"] = df["cpa_monthly_method"] != CPA_REAL

    # Note: the unit-economics inversion + CPA-vs-LTV alerts (§11) live in risk_classifier,
    # which owns thresholds. This module supplies their inputs (cpa_t12m here; ltv in the
    # economics frame) rather than computing the flag — see decisions_log (BS3 analytics core).
    cols = (UNIT + ["period", "cpa_monthly", "cpa_monthly_method", "cpa_t3", "cpa_t12",
                    "cpa_ref", "gl_completeness_state", "total_spend", "conversions_landed",
                    "is_projectable", "cpa_estimated"])
    return df[cols].rename(columns={"cpa_t3": "cpa_t3m", "cpa_t12": "cpa_t12m"})


# ===========================================================================
# 6. Fallout — leaf grain (raw unmatched share, with a pending-resolution flag)
# ===========================================================================
def _fallout(fallout: pd.DataFrame, *, snapshot_date: dt.date) -> pd.DataFrame:
    """``fallout_rate = unmatched ÷ submissions``, reported **raw** (pending cohorts included
    — BS3 decision). A cohort is ``pending_resolution`` until the snapshot is past its
    month-end by the conversion-lag SLA, i.e. recent gains may still land (a lagging signal)."""
    df = fallout.copy()
    df["fallout_rate"] = _safe_divide(df["unmatched"], df["submissions"])
    resolved_by = df["period"].map(
        lambda s: _period_end(s) + dt.timedelta(days=CONV_LAG_SLA_DAYS))
    df["pending_resolution"] = snapshot_date < resolved_by
    df["fallout_method"] = FALLOUT_REAL
    return df[DIMS + ["period", "submissions", "matched", "unmatched",
                      "fallout_rate", "pending_resolution", "fallout_method"]]


# ===========================================================================
# 7. Public entry points
# ===========================================================================
def calculate_metrics(merged: dict[str, pd.DataFrame], gl_states: pd.DataFrame,
                      cogs_config: pd.DataFrame, retention_config: pd.DataFrame,
                      *, snapshot_date: dt.date, period_close_day: int) -> dict[str, pd.DataFrame]:
    """Compute the four metrics + fallout from the merged frames and GL states.

    Returns three frames at their native grain (context doc §14):

        cpa        unit×period — monthly/T3M/T12M CPA, method, gl state, is_projectable, flags
        economics  leaf×period — COGS (+ comparison inputs), margin, LTV, each with method
        fallout    leaf×cohort-period — raw fallout rate + pending-resolution flag
    """
    snap_period = _snapshot_period(snapshot_date)

    # Economics spine = conversions leaf×period (carries price + reference columns).
    economics = merged["conversions_with_ref"].copy()
    economics = _cogs(economics, cogs_config)               # COGS first …
    economics = _margin(economics)                          # … then margin …
    economics = _ltv(economics, retention_config)           # … then LTV.
    economics["is_projectable"] = economics["period"] == snap_period
    economics["margin_estimated"] = economics["margin_method"] != MARGIN_CALC
    economics["ltv_estimated"] = ~economics["ltv_method"].isin([LTV_RETENTION, LTV_TERM])

    cpa = _cpa(merged["conversions_with_ref"], gl_states, snapshot_period=snap_period)
    fallout = _fallout(merged["fallout"], snapshot_date=snapshot_date)

    economics = economics[
        DIMS + ["period", "cogs_per_unit", "cogs_method", "cogs_actual", "cogs_plan",
                "cogs_forecast", "cogs_trailing3", "cogs_prior_year", "cogs_comparison_mode",
                "price_per_unit", "margin_per_unit", "margin_per_period", "margin_method",
                "ltv", "ltv_method", "margin_ref", "ltv_ref",
                "is_projectable", "margin_estimated", "ltv_estimated"]]

    logger.info("metrics: cpa %d×%d, economics %d×%d, fallout %d×%d",
                *cpa.shape, *economics.shape, *fallout.shape)
    return {"cpa": cpa, "economics": economics, "fallout": fallout}


def compute_metrics(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict[str, pd.DataFrame]:
    """Convenience entry: load → merge → process_gl → calculate, reading ``snapshot_date`` and
    ``period_close_day`` from system_config (mirrors ``gl_processor.process_gl``)."""
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames

    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    period_close_day = int(cfg["period_close_day"])

    data = load_data(config_path)
    merged = merge_frames(data)
    gl_states = process_gl(merged["gl_acquisition"], config_path)
    return calculate_metrics(merged, gl_states, data["cogs_config"], data["retention_config"],
                             snapshot_date=snapshot_date, period_close_day=period_close_day)


# ===========================================================================
# 8. CLI — manual verification aid (eyeball every field + every method label)
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    frames = compute_metrics()
    for name, df in frames.items():
        print(f"\n=== {name}  ({df.shape[0]:,} × {df.shape[1]}) ===")
        for col in [c for c in df.columns if c.endswith("_method")]:
            print(f"  {col}: {df[col].value_counts().to_dict()}")
        for col in [c for c in df.columns if c.endswith("_estimated") or c == "is_projectable"]:
            print(f"  {col}: {df[col].value_counts().to_dict()}")
        print(df.head(6).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
