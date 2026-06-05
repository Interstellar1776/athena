#!/usr/bin/env python3
"""risk_classifier.py — score every metric with a magnitude-only risk level.

Build Sequence §19 step 3 (context doc §15), after ``metrics_calculator`` + ``projection_engine``.
This is the judgment layer: it computes every variance and assigns a risk level
(HIGH/MEDIUM/LOW/INFO) to **every** metric, honestly labeled real / estimated / projection. It owns
the §11 alert stack. Its output is the raw material ``findings_builder`` rolls up into the §14 feed.

Decisions (see ``docs/decisions_log.md`` — BS3 analytics core, module 3):

* **Score everything.** One assessment row per metric × period × grain, each with a level —
  *including LOW / on-track*. Nothing is suppressed; the feed filters (full transparency).
* **Finest honest grain.** Leaf for COGS / margin / fallout / volume; **unit**
  ``(entity, region, segment)`` for CPA & CPA-vs-LTV (GL only resolves to unit). Each row carries a
  ``group_key`` so ``findings_builder`` can roll a multi-leaf event into one alert with drill-down.
* **6-month window.** Point alerts evaluated for each month in the window; trend alerts over it;
  restatement / accrual *updates* surfaced for prior months.
* **Severity = magnitude only; ``estimated`` is orthogonal** (§11). An estimated HIGH stays HIGH.
* **CPA-vs-LTV: compression on T3M, inversion on T12M** (the responsive vs slow-burn split).

Thresholds live in ``system_config.yaml`` (the ``thresholds`` block + ``cpa_ltv_warning_threshold``).

CLI:
    python -m app.analytics.risk_classifier
    # run the chain, print assessment counts by alert_type × risk_level + the non-LOW rows.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from app.analytics.data_merger import DIMS
from app.analytics.gl_processor import UNIT
from app.analytics.metrics_calculator import _snapshot_period  # noqa: F401 (period helper parity)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

HIGH, MEDIUM, LOW, INFO = "HIGH", "MEDIUM", "LOW", "INFO"
UNFAVORABLE, FAVORABLE, NEUTRAL = "UNFAVORABLE", "FAVORABLE", "NEUTRAL"
LEAF_ONLY = [c for c in DIMS if c not in UNIT]      # dims a unit row leaves blank

# Output schema — one row per scored metric, uniform across every alert type.
ASSESSMENT_COLUMNS = (
    DIMS + ["group_key", "grain", "period", "alert_type", "metric",
            "actual", "actual_method", "reference_value", "reference_type",
            "variance_pct", "variance_direction", "risk_level", "estimated",
            "confidence", "supporting"])

# Metric methods that are authoritative (not estimated).
REAL_METHODS = {"real", "calculated", "calculated_retention", "calculated_term", "actual"}


# ===========================================================================
# 1. Small shared helpers — window, banding, row standardization
# ===========================================================================
def _window_periods(snapshot_date: dt.date, n: int) -> list[str]:
    """The last ``n`` month-periods (``YYYY-MM``), inclusive of the snapshot's month."""
    cur = pd.Period(snapshot_date, freq="M")
    return [(cur - i).strftime("%Y-%m") for i in range(n)]


def _level(magnitude: float, med: float, high: float) -> str:
    """HIGH/MEDIUM/LOW from an unfavorable magnitude (≥0). NaN → LOW (nothing to flag)."""
    if magnitude is None or (isinstance(magnitude, float) and np.isnan(magnitude)):
        return LOW
    if magnitude >= high:
        return HIGH
    if magnitude >= med:
        return MEDIUM
    return LOW


def _directional(actual: pd.Series, reference: pd.Series, *, higher_is_bad: bool,
                 med: float, high: float) -> pd.DataFrame:
    """Variance vs a reference + risk band, given which direction is unfavorable.

    Returns columns ``variance_pct``, ``variance_direction``, ``risk_level``. Only the
    unfavorable side earns MEDIUM/HIGH; the favorable side is LOW (direction still labeled)."""
    var = (actual - reference) / reference.replace(0, np.nan)
    unfavorable = var > 0 if higher_is_bad else var < 0
    signed = var if higher_is_bad else -var                      # unfavorable side positive
    magnitude = signed.clip(lower=0)                             # 0 on the favorable side
    out = pd.DataFrame({
        "variance_pct": (var * 100).round(2),
        "variance_direction": np.where(var == 0, NEUTRAL,
                                np.where(unfavorable, UNFAVORABLE, FAVORABLE)),
        "risk_level": [_level(m, med, high) for m in magnitude],
    })
    return out


def _standardize(df: pd.DataFrame, *, grain: str) -> pd.DataFrame:
    """Ensure a rule's rows carry every ASSESSMENT_COLUMN. Unit rows blank the leaf-only dims;
    ``group_key`` is the unit identity (entity|region|segment) every row rolls up to."""
    df = df.copy()
    if grain == "unit":
        for c in LEAF_ONLY:
            df[c] = ""
    df["grain"] = grain
    df["group_key"] = df["entity"].str.cat([df["region"], df["segment"]], sep="|")
    for c in ASSESSMENT_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df[ASSESSMENT_COLUMNS]


def _estimated(method: pd.Series) -> pd.Series:
    return ~method.isin(REAL_METHODS)


def _confidence(estimated: pd.Series) -> pd.Series:
    return np.where(estimated, "low", "high")


# ===========================================================================
# 2. Acquisition alerts — CPA spike / trend, volume miss, fallout
# ===========================================================================
def _cpa_spike(cpa: pd.DataFrame, thr: dict, periods: list[str]) -> pd.DataFrame:
    """Monthly CPA vs plan cpa_ref (unit). Higher is bad."""
    df = cpa[cpa["period"].isin(periods) & cpa["cpa_ref"].notna()].copy().reset_index(drop=True)
    band = _directional(df["cpa_monthly"], df["cpa_ref"], higher_is_bad=True,
                        med=thr["cpa_spike"]["medium"], high=thr["cpa_spike"]["high"])
    df = df.join(band)
    df["alert_type"] = "cpa_spike"
    df["metric"] = "cost_per_acquisition"
    df["actual"] = df["cpa_monthly"]
    df["actual_method"] = df["cpa_monthly_method"]
    df["reference_value"] = df["cpa_ref"]
    df["reference_type"] = "plan"
    df["estimated"] = _estimated(df["cpa_monthly_method"])
    df["confidence"] = _confidence(df["estimated"])
    df["supporting"] = df.apply(lambda r: {"cpa_t3m": r["cpa_t3m"], "cpa_t12m": r["cpa_t12m"],
                                           "total_spend": r["total_spend"],
                                           "conversions_landed": r["conversions_landed"],
                                           "gl_state": r["gl_completeness_state"]}, axis=1)
    return _standardize(df, grain="unit")


def _cpa_trend(cpa: pd.DataFrame, thr: dict, periods: list[str]) -> pd.DataFrame:
    """Monthly CPA rising N consecutive months → MEDIUM (unit), evaluated at the latest period."""
    n = int(thr["cpa_trend_months"])
    rows = []
    latest = periods[0]
    for keys, g in cpa[cpa["period"].isin(periods)].groupby(UNIT, observed=True):
        g = g.sort_values("period")
        series = g["cpa_monthly"].to_numpy(dtype=float)
        rising = len(series) >= n and all(np.diff(series[-n:]) > 0)
        last = g.iloc[-1]
        rows.append({**dict(zip(UNIT, keys if isinstance(keys, tuple) else (keys,))),
                     "period": latest, "actual": last["cpa_monthly"],
                     "actual_method": last["cpa_monthly_method"],
                     "reference_value": np.nan, "reference_type": "trend",
                     "variance_pct": np.nan,
                     "variance_direction": UNFAVORABLE if rising else NEUTRAL,
                     "risk_level": MEDIUM if rising else LOW,
                     "estimated": bool(_estimated(pd.Series([last["cpa_monthly_method"]]))[0]),
                     "confidence": "high",
                     "supporting": {"last_n_cpa": series[-n:].tolist()}})
    df = pd.DataFrame(rows)
    df["alert_type"] = "cpa_trend"
    df["metric"] = "cost_per_acquisition"
    return _standardize(df, grain="unit")


def _volume_miss(projection: pd.DataFrame, thr: dict, established: set) -> pd.DataFrame:
    """Projected period-end converted vs plan (leaf, current period). Lower is bad. Two lines.

    A **first-run** leaf (launched this period — no prior history) has a full-month plan but only
    post-launch actuals, so the projected 'miss' overstates the real shortfall (a structurally
    imperfect comparison — pro-rating the launch-month plan is a deferred fix). Per the flag-don't-
    suppress policy it is still flagged at its magnitude, but tagged ``first_run`` + low confidence
    so a reviewer sees it for what it is rather than having it silently dropped."""
    vp = projection
    if vp.empty:
        return _standardize(_empty_with_dims("leaf"), grain="leaf")
    is_first_run = ~vp[DIMS].apply(tuple, axis=1).isin(established)
    out = []
    for line in ("linear", "weighted"):
        df = vp.copy().reset_index(drop=True)
        band = _directional(df[f"converted_proj_{line}"], df["converted_plan_full"],
                            higher_is_bad=False,
                            med=thr["volume_miss"]["medium"], high=thr["volume_miss"]["high"])
        df = df.join(band)
        df.loc[is_first_run.values, "confidence"] = "low"        # flagged, but low-confidence
        df["alert_type"] = f"volume_miss_{line}"
        df["metric"] = "volume_converted"
        df["actual"] = df[f"converted_proj_{line}"]
        df["actual_method"] = "projection_" + df["converted_proj_method"].astype(str)
        df["reference_value"] = df["converted_plan_full"]
        df["reference_type"] = "plan"
        df["estimated"] = True                                   # a projection is always estimated
        df["first_run"] = is_first_run.values
        df["supporting"] = df.apply(lambda r: {"to_date": r["converted_to_date"],
                                               "plan_prorated": r["converted_plan_prorated"],
                                               "days": f"{r['days_elapsed']}/{r['days_in_period']}",
                                               "method": r["converted_proj_method"],
                                               "first_run": bool(r["first_run"])}, axis=1)
        out.append(_standardize(df, grain="leaf"))
    return pd.concat(out, ignore_index=True)


def _fallout(fallout: pd.DataFrame, fallout_proj: pd.DataFrame, thr: dict, periods: list[str],
             current_period: str) -> pd.DataFrame:
    """Fallout vs the channel's OWN trailing-3-month baseline (leaf). Higher is bad.

    Absolute fallout is a poor signal here (every channel runs above its optimistic plan); the real
    signal is *degradation vs the leaf's own recent norm*. The current period is made **proactive**
    by using ``projection_engine``'s resolved-sub-cohort rate (lag-corrected) in place of the raw
    pending-inflated value — so the degradation flags *before close* (e.g. Telemarketing ~+50% at
    May-22, escalating to HIGH once the cohort fully resolves). Prior months use their final rate.
    Everything with a baseline is flagged (no suppression — the confidence label carries the
    uncertainty); a leaf with no baseline can't be compared, so it stays LOW."""
    fo = fallout.sort_values(DIMS + ["period"]).copy()
    fo["_baseline"] = (fo.groupby(DIMS, observed=True)["fallout_rate"]
                       .transform(lambda s: s.rolling(3, min_periods=1).mean().shift(1)))
    df = fo[fo["period"].isin(periods)].copy()

    # Override the current period's rate with the proactive (resolved-sub-cohort) projection.
    if fallout_proj is not None and len(fallout_proj):
        proj = fallout_proj[DIMS + ["fallout_rate", "fallout_method", "confidence"]].rename(
            columns={"fallout_rate": "_p_rate", "fallout_method": "_p_method", "confidence": "_p_conf"})
        df = df.merge(proj, on=DIMS, how="left")
    else:
        df = df.assign(_p_rate=np.nan, _p_method=None, _p_conf=None)
    is_cur = (df["period"] == current_period) & df["_p_rate"].notna()
    df["_rate"] = np.where(is_cur, df["_p_rate"], df["fallout_rate"])
    df["_method"] = np.where(is_cur, df["_p_method"], "real")
    df["_conf"] = np.where(is_cur, df["_p_conf"], "high")
    df = df.reset_index(drop=True)

    med, high = thr["fallout_rate"]["medium"], thr["fallout_rate"]["high"]
    rel = (df["_rate"] - df["_baseline"]) / df["_baseline"].replace(0, np.nan)
    # Band only with a baseline AND enough data. A current cohort too thin to resolve falls back to the
    # plain (pending-inflated) rate, labelled `plain_no_data` — that rate is unreliable (≈100% early in
    # the period), so it must NOT fire an alert (owner: "if not enough data, it shouldn't flag"). It still
    # carries the plain value + no_data/low confidence in the table for review.
    bandable = df["_baseline"].notna() & (df["_method"] != "plain_no_data")
    df["risk_level"] = [(_level(r, med, high) if ok else LOW)
                        for r, ok in zip(rel.fillna(0), bandable)]
    df["alert_type"] = "fallout_rate"
    df["metric"] = "fallout_rate"
    df["actual"] = df["_rate"].round(4)
    df["actual_method"] = df["_method"]
    df["reference_value"] = df["_baseline"].round(4)
    df["reference_type"] = "trailing_baseline"
    df["variance_pct"] = (rel * 100).round(2)
    df["variance_direction"] = np.where((rel > 0) & bandable, UNFAVORABLE,
                                np.where(rel < 0, FAVORABLE, NEUTRAL))
    df["estimated"] = df["_method"] != "real"
    df["confidence"] = df["_conf"]
    df["supporting"] = df.apply(lambda r: {"submissions": r["submissions"], "unmatched": r["unmatched"],
                                           "trailing_baseline": round(r["_baseline"], 4)
                                           if pd.notna(r["_baseline"]) else None,
                                           "method": r["_method"]}, axis=1)
    return _standardize(df, grain="leaf")


# ===========================================================================
# 3. Unit-economics alerts — CPA-vs-LTV inversion / compression, margin
# ===========================================================================
def _unit_ltv(economics: pd.DataFrame) -> pd.DataFrame:
    """Roll leaf LTV to unit grain (mean — LTV is uniform within a unit) for CPA-vs-LTV."""
    return (economics.groupby(UNIT + ["period"], observed=True)
            .agg(unit_ltv=("ltv", "mean"), ltv_method=("ltv_method", "first")).reset_index())


def _cpa_ltv(cpa: pd.DataFrame, economics: pd.DataFrame, thr: dict, cfg: dict,
             periods: list[str]) -> pd.DataFrame:
    """Inversion on T12M CPA / LTV (HIGH ≥ 1.0); compression on T3M CPA / LTV (MEDIUM ≥ 0.80)."""
    ltv = _unit_ltv(economics)
    df = cpa[cpa["period"].isin(periods)].merge(ltv, on=UNIT + ["period"], how="left")
    df = df[df["unit_ltv"].notna() & (df["unit_ltv"] > 0)].reset_index(drop=True)
    est = _estimated(df["cpa_monthly_method"]) | _estimated(df["ltv_method"])

    out = []
    # Inversion (T12M) — hard, HIGH.
    inv_ratio = df["cpa_t12m"] / df["unit_ltv"]
    inv = df.copy()
    inv["alert_type"] = "cpa_ltv_inversion"; inv["metric"] = "t12m_cpa_over_ltv"
    inv["actual"] = df["cpa_t12m"]; inv["actual_method"] = df["cpa_monthly_method"]
    inv["reference_value"] = df["unit_ltv"]; inv["reference_type"] = "ltv"
    inv["variance_pct"] = (inv_ratio * 100).round(2)
    inv["variance_direction"] = np.where(inv_ratio >= 1.0, UNFAVORABLE, FAVORABLE)
    inv["risk_level"] = np.where(inv_ratio >= float(thr["cpa_ltv_inversion"]), HIGH, LOW)
    inv["estimated"] = est; inv["confidence"] = _confidence(est)
    inv["supporting"] = [{"cpa_t12m": a, "ltv": b, "ratio": round(r, 3)}
                         for a, b, r in zip(df["cpa_t12m"], df["unit_ltv"], inv_ratio)]
    out.append(_standardize(inv, grain="unit"))

    # Compression (T3M) — responsive, caps at MEDIUM.
    comp_ratio = df["cpa_t3m"] / df["unit_ltv"]
    warn = float(cfg["cpa_ltv_warning_threshold"])
    comp = df.copy()
    comp["alert_type"] = "cpa_ltv_compression"; comp["metric"] = "t3m_cpa_over_ltv"
    comp["actual"] = df["cpa_t3m"]; comp["actual_method"] = df["cpa_monthly_method"]
    comp["reference_value"] = df["unit_ltv"]; comp["reference_type"] = "ltv"
    comp["variance_pct"] = (comp_ratio * 100).round(2)
    comp["variance_direction"] = np.where(comp_ratio >= warn, UNFAVORABLE, FAVORABLE)
    comp["risk_level"] = np.where(comp_ratio >= warn, MEDIUM, LOW)
    comp["estimated"] = est; comp["confidence"] = _confidence(est)
    comp["supporting"] = [{"cpa_t3m": a, "ltv": b, "ratio": round(r, 3), "threshold": warn}
                          for a, b, r in zip(df["cpa_t3m"], df["unit_ltv"], comp_ratio)]
    out.append(_standardize(comp, grain="unit"))
    return pd.concat(out, ignore_index=True)


def _margin(economics: pd.DataFrame, thr: dict, periods: list[str]) -> pd.DataFrame:
    """Margin per unit vs plan margin_ref (leaf). Lower is bad."""
    df = (economics[economics["period"].isin(periods) & economics["margin_ref"].notna()]
          .copy().reset_index(drop=True))
    band = _directional(df["margin_per_unit"], df["margin_ref"], higher_is_bad=False,
                        med=thr["margin_compression"]["medium"],
                        high=thr["margin_compression"]["high"])
    df = df.join(band)
    df["alert_type"] = "margin_compression"; df["metric"] = "margin_per_unit"
    df["actual"] = df["margin_per_unit"]; df["actual_method"] = df["margin_method"]
    df["reference_value"] = df["margin_ref"]; df["reference_type"] = "plan"
    df["estimated"] = _estimated(df["margin_method"]); df["confidence"] = _confidence(df["estimated"])
    df["supporting"] = df.apply(lambda r: {"price_per_unit": r["price_per_unit"],
                                           "cogs_per_unit": r["cogs_per_unit"]}, axis=1)
    return _standardize(df, grain="leaf")


# ===========================================================================
# 4. Cost alerts — COGS spike (per comparison mode), GL updates (late/restatement)
# ===========================================================================
_COGS_BASELINE = {"plan_vs_actual": "cogs_plan", "hybrid": "cogs_plan",
                  "prior_year_same_period": "cogs_prior_year", "linear_trend": "cogs_trailing3"}


def _cogs_spike(economics: pd.DataFrame, thr: dict, periods: list[str]) -> pd.DataFrame:
    """cogs_actual vs the baseline its cogs_comparison_mode selects (leaf). Higher is bad."""
    df = economics[economics["period"].isin(periods) & economics["cogs_actual"].notna()].copy()
    baseline_col = df["cogs_comparison_mode"].map(_COGS_BASELINE).fillna("cogs_plan")
    df["_baseline"] = [df.at[i, c] if c in df.columns else np.nan
                       for i, c in zip(df.index, baseline_col)]
    df = df[df["_baseline"].notna()].reset_index(drop=True)
    band = _directional(df["cogs_actual"], df["_baseline"], higher_is_bad=True,
                        med=thr["cogs_spike"]["medium"], high=thr["cogs_spike"]["high"])
    df = df.join(band)
    df["alert_type"] = "cogs_spike"; df["metric"] = "cogs_per_unit"
    df["actual"] = df["cogs_actual"]; df["actual_method"] = df["cogs_method"]
    df["reference_value"] = df["_baseline"]
    df["reference_type"] = df["cogs_comparison_mode"]
    df["estimated"] = _estimated(df["cogs_method"]); df["confidence"] = _confidence(df["estimated"])
    df["supporting"] = df.apply(lambda r: {"mode": r["cogs_comparison_mode"], "plan": r["cogs_plan"],
                                           "trailing3": r["cogs_trailing3"],
                                           "prior_year": r["cogs_prior_year"]}, axis=1)
    return _standardize(df, grain="leaf")


def _gl_updates(gl_states: pd.DataFrame, cpa: pd.DataFrame, thr: dict,
                periods: list[str]) -> pd.DataFrame:
    """Late-invoice (INFO) + restatement/accrual updates (MEDIUM by CPA impact), per (unit, period).

    Stateless restatement (Option B): the CPA change caused by late/accrued spend =
    late_invoice_amount / conversions; the frozen (pre-update) CPA excludes that spend."""
    gl = gl_states[gl_states["period"].isin(periods) & (gl_states["late_invoice_count"] > 0)].copy()
    if gl.empty:
        return _standardize(_empty_with_dims("unit"), grain="unit")
    conv = cpa[["entity", "region", "segment", "period", "conversions_landed",
                "cpa_monthly"]].copy()
    gl = gl.merge(conv, on=UNIT + ["period"], how="left")
    gl["restatement_delta"] = gl["late_invoice_amount"] / gl["conversions_landed"].replace(0, np.nan)
    frozen = gl["cpa_monthly"] - gl["restatement_delta"]
    gl["variance_pct"] = (gl["restatement_delta"] / frozen.replace(0, np.nan) * 100).round(2)
    med = thr["restatement_cpa"]["medium"]
    gl["risk_level"] = [MEDIUM if (abs(v) / 100) >= med else INFO for v in gl["variance_pct"].fillna(0)]
    gl["alert_type"] = "restatement"; gl["metric"] = "cost_per_acquisition"
    gl["actual"] = gl["cpa_monthly"]; gl["actual_method"] = "real"
    gl["reference_value"] = frozen; gl["reference_type"] = "frozen_reference"
    gl["variance_direction"] = np.where(gl["restatement_delta"] > 0, UNFAVORABLE, FAVORABLE)
    gl["estimated"] = False; gl["confidence"] = "high"
    gl["frozen_reference"] = frozen
    gl["supporting"] = gl.apply(
        lambda r: {"gl_state": r["gl_completeness_state"],
                   "late_invoice_amount": round(r["late_invoice_amount"], 2),
                   "late_invoice_count": int(r["late_invoice_count"]),
                   "restatement_delta": round(r["restatement_delta"], 2)
                   if pd.notna(r["restatement_delta"]) else None,
                   "frozen_reference": round(r["frozen_reference"], 2)
                   if pd.notna(r["frozen_reference"]) else None}, axis=1)
    return _standardize(gl, grain="unit")


# ===========================================================================
# 5. Projection alert — plan vs forecast (unit CPA)
# ===========================================================================
def _plan_vs_forecast(reference: pd.DataFrame, thr: dict, periods: list[str]) -> pd.DataFrame:
    """Plan cpa_ref vs forecast cpa_ref (unit). Divergence over threshold → MEDIUM."""
    ref = reference.copy()
    ref["period"] = pd.to_datetime(ref["date"]).dt.to_period("M").astype(str)
    plan = (ref[ref["reference_type"] == "plan"].groupby(UNIT + ["period"], observed=True)
            ["cpa_ref"].mean().rename("plan_cpa"))
    fc = (ref[ref["reference_type"] == "forecast"].groupby(UNIT + ["period"], observed=True)
          ["cpa_ref"].mean().rename("fc_cpa"))
    df = pd.concat([plan, fc], axis=1).dropna().reset_index()
    if df.empty:
        return _standardize(_empty_with_dims("unit"), grain="unit")
    df["divergence"] = (df["fc_cpa"] - df["plan_cpa"]).abs() / df["plan_cpa"]
    med = thr["plan_vs_forecast"]["medium"]
    df["alert_type"] = "plan_vs_forecast_gap"; df["metric"] = "cost_per_acquisition"
    df["actual"] = df["fc_cpa"]; df["actual_method"] = "forecast"
    df["reference_value"] = df["plan_cpa"]; df["reference_type"] = "plan"
    df["variance_pct"] = (df["divergence"] * 100).round(2)
    df["variance_direction"] = np.where(df["fc_cpa"] > df["plan_cpa"], UNFAVORABLE, FAVORABLE)
    df["risk_level"] = np.where(df["divergence"] >= med, MEDIUM, LOW)
    df["estimated"] = True; df["confidence"] = "medium"
    df["supporting"] = [{"plan_cpa": p, "forecast_cpa": f} for p, f in zip(df["plan_cpa"], df["fc_cpa"])]
    return _standardize(df, grain="unit")


def _empty_with_dims(grain: str) -> pd.DataFrame:
    cols = (UNIT if grain == "unit" else DIMS) + ["period"]
    return pd.DataFrame(columns=cols)


# ===========================================================================
# 6. Public entry points
# ===========================================================================
def classify(metrics: dict[str, pd.DataFrame], projection: dict[str, pd.DataFrame],
             gl_states: pd.DataFrame, reference: pd.DataFrame, *, snapshot_date: dt.date,
             thresholds: dict, cpa_ltv_warning_threshold: float = 0.80,
             window_months: int = 6) -> dict[str, pd.DataFrame]:
    """Score every metric across the trailing ``window_months`` and return one assessment table."""
    periods = _window_periods(snapshot_date, window_months)
    cpa, econ, fo = metrics["cpa"], metrics["economics"], metrics["fallout"]
    vp = projection["volume_projection"]
    cfg = {"cpa_ltv_warning_threshold": cpa_ltv_warning_threshold}

    # First-run guard for volume_miss: leaves with any economics row in a period BEFORE the
    # current one are "established"; the rest launched this period (full-month plan ≠ actuals).
    current_period = periods[0]
    established = set(econ.loc[econ["period"] < current_period, DIMS].apply(tuple, axis=1))

    parts = [
        _cpa_spike(cpa, thresholds, periods),
        _cpa_trend(cpa, thresholds, periods),
        _volume_miss(vp, thresholds, established),
        _fallout(fo, projection.get("fallout_projection"), thresholds, periods, current_period),
        _cpa_ltv(cpa, econ, thresholds, cfg, periods),
        _margin(econ, thresholds, periods),
        _cogs_spike(econ, thresholds, periods),
        _gl_updates(gl_states, cpa, thresholds, periods),
        _plan_vs_forecast(reference, thresholds, periods),
    ]
    assessments = pd.concat(parts, ignore_index=True)
    logger.info("risk_classifier: %d assessments; by level=%s",
                len(assessments), assessments["risk_level"].value_counts().to_dict())
    return {"assessments": assessments}


def compute_assessments(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict[str, pd.DataFrame]:
    """Convenience entry: run the upstream chain and read thresholds from system_config."""
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames
    from app.analytics.gl_processor import process_gl
    from app.analytics.metrics_calculator import calculate_metrics
    from app.analytics.projection_engine import project_fallout, project_volume

    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    period_close_day = int(cfg["period_close_day"])
    data = load_data(config_path)
    merged = merge_frames(data)
    gl_states = process_gl(merged["gl_acquisition"], config_path)
    metrics = calculate_metrics(merged, gl_states, data["cogs_config"], data["retention_config"],
                                snapshot_date=snapshot_date, period_close_day=period_close_day)
    projection = {**project_volume(data, merged, metrics, snapshot_date=snapshot_date,
                                   pro_rate=str(cfg.get("pro_rate_default", "calendar_days"))),
                  **project_fallout(data, metrics, snapshot_date=snapshot_date)}
    return classify(metrics, projection, gl_states, data["reference_data"],
                    snapshot_date=snapshot_date, thresholds=cfg["thresholds"],
                    cpa_ltv_warning_threshold=float(cfg["cpa_ltv_warning_threshold"]))


# ===========================================================================
# 7. CLI — manual verification aid
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    a = compute_assessments()["assessments"]
    print(f"\n=== assessments ({len(a):,} rows) ===")
    print(pd.crosstab(a["alert_type"], a["risk_level"]).to_string())
    flagged = a[a["risk_level"].isin([HIGH, MEDIUM, INFO])]
    print(f"\n--- non-LOW ({len(flagged)}) ---")
    show = ["alert_type", "region", "segment", "period", "actual", "reference_value",
            "variance_pct", "risk_level", "estimated"]
    print(flagged.sort_values(["risk_level", "alert_type"])[show].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
