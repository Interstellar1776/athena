#!/usr/bin/env python3
"""findings_builder.py — assemble the §14 structured findings from the assessment table.

Build Sequence §19 step 3 (context doc §15), after ``risk_classifier``. The deterministic bridge
(no LLM) from the flat **assessment table** (every metric × period × grain, scored) to the
**structured findings** that are the system contract (§14) every downstream module reads.

What it does (decisions in ``docs/decisions_log.md`` — BS3 analytics core, module 4 + refinements):

* **Non-LOW become findings.** HIGH/MEDIUM/INFO assessments → §14 findings; the full assessment table
  travels alongside as the browse / drill-down layer.
* **One finding per (alert_type, unit, period).** Leaf-grain alerts roll leaf→unit; every leaf is
  nested under ``supporting_metrics.leaves``. **Volume is one finding** carrying both the linear and
  weighted period-end projections (not two).
* **Headline = the unit aggregate** (not the worst leaf): fallout submission-weighted, volume summed,
  COGS/margin mean; the finding's primary-metric context field is set to that aggregate so they agree.
  **Severity stays max-leaf** (a leaf-level HIGH still makes the unit finding HIGH — don't-miss); the
  worst leaf is always in the drill-down.
* **Rank by normalized exceedance** — magnitude ÷ the alert's own crossed threshold — so different
  alert types are comparable (and the CPA-vs-LTV ratio is normalized, not treated as a raw %).
* **Bundle context** (§14): COGS/LTV/margin + methods, ``unit_economics_flag``,
  ``gl_completeness_state``, projections, ``frozen_reference`` / ``restatement_delta``,
  ``supporting_metrics``.

CLI:
    python -m app.analytics.findings_builder
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
from app.analytics.metrics_calculator import _snapshot_period
from app.analytics.risk_classifier import HIGH, INFO, LOW, MEDIUM  # noqa: F401

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

FLAGGED = {HIGH, MEDIUM, INFO}
SEVERITY_RANK = {HIGH: 0, MEDIUM: 1, INFO: 2, LOW: 3}
LEAF_ONLY = [c for c in DIMS if c not in UNIT]
VOLUME_LINES = {"volume_miss_linear", "volume_miss_weighted"}
VOLUME = "volume_miss"

# Per-alert recipe for aggregation + exceedance — one explicit table so feed semantics can't drift.
#   kind:   how the magnitude relates to the threshold ("two_band" / "single" / "ratio" / "trend")
#   thr:    threshold key in the config thresholds block, or "warn"/"inversion" for the CPA-vs-LTV pair
#   weight: how leaf rows roll to the unit headline ("none"=already unit, "mean", "submissions", "sum")
ALERT_SPEC = {
    "cpa_spike":            ("two_band", "cpa_spike", "none"),
    "cpa_trend":            ("trend", None, "none"),
    "fallout_rate":         ("two_band", "fallout_rate", "submissions"),
    "cpa_ltv_inversion":    ("ratio", "inversion", "none"),
    "cpa_ltv_compression":  ("ratio", "warn", "none"),
    "margin_compression":   ("two_band", "margin_compression", "mean"),
    "cogs_spike":           ("two_band", "cogs_spike", "mean"),
    "restatement":          ("single", "restatement_cpa", "none"),
    "plan_vs_forecast_gap": ("single", "plan_vs_forecast", "none"),
    VOLUME:                 ("two_band", "volume_miss", "sum"),
}
# Which §14 context field a finding's primary metric writes back to (so actual == context).
PRIMARY_CONTEXT = {"margin_compression": "margin_per_unit", "cogs_spike": "cogs_per_unit"}


# ===========================================================================
# 1. Context lookups — built once, keyed by (unit, period)
# ===========================================================================
def _unit_economics(economics: pd.DataFrame) -> dict:
    g = economics.groupby(UNIT + ["period"], observed=True)
    rolled = g.agg(cogs_per_unit=("cogs_per_unit", "mean"), cogs_method=("cogs_method", "first"),
                   ltv=("ltv", "mean"), ltv_method=("ltv_method", "first"),
                   margin_per_unit=("margin_per_unit", "mean"),
                   margin_method=("margin_method", "first")).reset_index()
    return {tuple(r[c] for c in UNIT + ["period"]): r for _, r in rolled.iterrows()}


def _unit_cpa(cpa: pd.DataFrame) -> dict:
    return {tuple(r[c] for c in UNIT + ["period"]): r for _, r in cpa.iterrows()}


def _unit_economics_flags(assessments: pd.DataFrame) -> set:
    inv = assessments[(assessments["alert_type"] == "cpa_ltv_inversion") &
                      (assessments["risk_level"] == HIGH)]
    return {tuple(r[c] for c in UNIT + ["period"]) for _, r in inv.iterrows()}


def _day_counts(volume_proj: pd.DataFrame, current_period: str) -> dict:
    out = {}
    if volume_proj is not None and len(volume_proj):
        row = volume_proj.iloc[0]
        out[current_period] = (int(row["days_elapsed"]), int(row["days_in_period"]))
    return out


def _days_for(period: str, day_counts: dict) -> tuple[int, int]:
    if period in day_counts:
        return day_counts[period]
    n = pd.Period(period, freq="M").days_in_month
    return n, n


# ===========================================================================
# 2. Aggregation (leaf→unit headline) + normalized exceedance (ranking)
# ===========================================================================
def _worst_leaf(group: pd.DataFrame) -> pd.Series:
    """Severity-driving row: max severity, then max |variance| — keeps don't-miss intact."""
    g = group.copy()
    g["_sev"] = g["risk_level"].map(SEVERITY_RANK)
    g["_mag"] = g["variance_pct"].abs().fillna(0.0)
    return g.sort_values(["_sev", "_mag"], ascending=[True, False]).iloc[0]


def _aggregate(group: pd.DataFrame, weight: str) -> tuple[float, float, float]:
    """Unit-level (actual, reference, variance_pct) per the weight rule (§ headline = unit aggregate)."""
    if weight == "none":                                     # already unit-grain (single row)
        r = group.iloc[0]
        return r["actual"], r["reference_value"], r["variance_pct"]
    actual = group["actual"].astype(float)
    ref = group["reference_value"].astype(float)
    if weight == "submissions":
        w = group["supporting"].map(lambda s: (s or {}).get("submissions", 0)).astype(float)
        if w.sum() == 0:                                     # guard: fall back to equal weights
            w = pd.Series(1.0, index=w.index)
        a, rf = float((actual * w).sum() / w.sum()), float((ref * w).sum() / w.sum())
    elif weight == "sum":
        a, rf = float(actual.sum()), float(ref.sum())
    else:                                                    # mean (uniform within a unit today)
        a, rf = float(actual.mean()), float(ref.mean())
    var = (a - rf) / rf * 100 if rf else np.nan
    return a, rf, round(var, 2)


def _exceedance(alert_type: str, variance_pct: float, risk_level: str,
                thresholds: dict, warn: float) -> float:
    """How far past its OWN threshold a finding sits — comparable across alert types. ≥1 = past the
    crossed cut; bigger = worse. The CPA-vs-LTV ratio is normalized here (ratio ÷ threshold), not
    treated as a percentage."""
    kind, key, _ = ALERT_SPEC[alert_type]
    if kind == "trend" or variance_pct is None or pd.isna(variance_pct):
        return 1.0                                           # at-threshold (no magnitude to scale)
    if kind == "ratio":
        ratio = variance_pct / 100.0
        threshold = warn if key == "warn" else float(thresholds["cpa_ltv_inversion"])
        return ratio / threshold if threshold else 1.0
    band = thresholds[key]
    crossed = band["high"] if risk_level == HIGH else band.get("medium", band.get("high"))
    return (abs(variance_pct) / 100.0) / crossed if crossed else 1.0


def _leaf_rows(group: pd.DataFrame) -> list[dict]:
    return [{**{c: r[c] for c in DIMS}, "actual": r["actual"], "variance_pct": r["variance_pct"],
             "risk_level": r["risk_level"], "supporting": r["supporting"]}
            for _, r in group.iterrows()]


# ===========================================================================
# 3. Assemble one §14 finding
# ===========================================================================
def _assemble(headline: pd.Series, leaves: list[dict], *, alert_type: str, actual: float,
              reference: float, variance_pct: float, exceedance: float,
              projections: tuple | None, econ: dict, cpa_ctx: dict, econ_flags: set,
              day_counts: dict, current_period: str) -> dict:
    period = headline["period"]
    unit_key = (headline["entity"], headline["region"], headline["segment"], period)
    days_elapsed, days_in = _days_for(period, day_counts)
    e, c = econ.get(unit_key), cpa_ctx.get(unit_key)
    is_restatement = alert_type == "restatement"
    gl_state = c["gl_completeness_state"] if c is not None else None
    if not isinstance(gl_state, str):                        # nan (no posted GL) → None, not a float
        gl_state = None

    finding = {
        "finding_id": None,
        "entity": headline["entity"], "region": headline["region"], "segment": headline["segment"],
        **{c2: "" for c2 in LEAF_ONLY},                      # rolled unit finding — spread is in leaves
        "metric": headline["metric"], "alert_type": alert_type, "grain": headline["grain"],
        "period": period, "days_elapsed": days_elapsed, "days_in_period": days_in,
        "confidence": headline["confidence"], "is_projectable": period == current_period,

        "actual": round(actual, 4) if actual is not None and pd.notna(actual) else None,
        "actual_method": headline["actual_method"],
        "reference_value": round(reference, 4) if reference is not None and pd.notna(reference) else None,
        "reference_type": headline["reference_type"],
        "variance_pct": variance_pct, "variance_direction": headline["variance_direction"],
        "risk_level": headline["risk_level"], "estimated": bool(headline["estimated"]),

        "projected_period_end_linear": projections[0] if projections else None,
        "projected_period_end_weighted": projections[1] if projections else None,

        "cogs_per_unit": round(e["cogs_per_unit"], 4) if e is not None else None,
        "cogs_method": e["cogs_method"] if e is not None else None,
        "ltv": round(e["ltv"], 2) if e is not None and pd.notna(e["ltv"]) else None,
        "ltv_method": e["ltv_method"] if e is not None else None,
        "margin_per_unit": round(e["margin_per_unit"], 2) if e is not None else None,
        "margin_method": e["margin_method"] if e is not None else None,
        "unit_economics_flag": unit_key in econ_flags,

        "gl_completeness_state": gl_state,
        "frozen_reference": (round(reference, 2) if is_restatement else None),
        "restatement_delta": (round(actual - reference, 2) if is_restatement else None),

        "supporting_metrics": {**(headline["supporting"] or {}), "grain": headline["grain"],
                               "leaf_count": len(leaves), "exceedance": round(exceedance, 3),
                               "leaves": leaves},
        "retrieved_context": "", "narrative": "", "validated": False, "validation_flags": [],
    }
    # Align the primary metric's context field to the headline aggregate (so actual == context).
    ctx_field = PRIMARY_CONTEXT.get(alert_type)
    if ctx_field and finding["actual"] is not None:
        finding[ctx_field] = finding["actual"]
    return finding


def _build_volume_finding(group: pd.DataFrame, **ctx) -> dict:
    """One volume_miss finding per unit carrying BOTH projections (linear + weighted)."""
    lin = group[group["alert_type"] == "volume_miss_linear"]
    wt = group[group["alert_type"] == "volume_miss_weighted"]
    plan_sum = float(wt["reference_value"].astype(float).sum()) or float(lin["reference_value"].astype(float).sum())
    lin_sum, wt_sum = float(lin["actual"].sum()), float(wt["actual"].sum())
    var_wt = (wt_sum - plan_sum) / plan_sum * 100 if plan_sum else np.nan
    headline = _worst_leaf(group)                            # severity from the worse line/leaf
    exc = _exceedance(VOLUME, var_wt, headline["risk_level"], ctx["thresholds"], ctx["warn"])
    leaves = _leaf_rows(wt if len(wt) else lin)
    f = _assemble(headline, leaves, alert_type=VOLUME, actual=wt_sum, reference=plan_sum,
                  variance_pct=round(var_wt, 2), exceedance=exc,
                  projections=(round(lin_sum, 2), round(wt_sum, 2)),
                  econ=ctx["econ"], cpa_ctx=ctx["cpa_ctx"], econ_flags=ctx["econ_flags"],
                  day_counts=ctx["day_counts"], current_period=ctx["current_period"])
    f["metric"] = "volume_converted"
    return f


# ===========================================================================
# 4. Public entry points
# ===========================================================================
def build_findings(assessments: pd.DataFrame, metrics: dict[str, pd.DataFrame],
                   projection: dict[str, pd.DataFrame], *, snapshot_date: dt.date,
                   thresholds: dict, cpa_ltv_warning_threshold: float = 0.80) -> list[dict]:
    """Select non-LOW assessments, roll into §14 findings (unit-aggregate headline, volume merged),
    rank by exceedance, assign ids."""
    current_period = _snapshot_period(snapshot_date)
    ctx = dict(econ=_unit_economics(metrics["economics"]), cpa_ctx=_unit_cpa(metrics["cpa"]),
               econ_flags=_unit_economics_flags(assessments),
               day_counts=_day_counts(projection.get("volume_projection"), current_period),
               current_period=current_period, thresholds=thresholds, warn=cpa_ltv_warning_threshold)

    flagged = assessments[assessments["risk_level"].isin(FLAGGED)]
    findings: list[dict] = []

    # Volume: merge the two projection lines into one finding per (unit, period).
    vol = flagged[flagged["alert_type"].isin(VOLUME_LINES)]
    for _, group in vol.groupby(["group_key", "period"], sort=False):
        findings.append(_build_volume_finding(group, **ctx))

    # Everything else: one finding per (alert_type, unit, period), unit-aggregate headline.
    rest = flagged[~flagged["alert_type"].isin(VOLUME_LINES)]
    for (alert_type, _, _), group in rest.groupby(["alert_type", "group_key", "period"], sort=False):
        headline = _worst_leaf(group)
        _, _, weight = ALERT_SPEC[alert_type]
        actual, reference, variance_pct = _aggregate(group, weight)
        exc = _exceedance(alert_type, variance_pct, headline["risk_level"], thresholds,
                          cpa_ltv_warning_threshold)
        findings.append(_assemble(headline, _leaf_rows(group), alert_type=alert_type, actual=actual,
                                  reference=reference, variance_pct=variance_pct, exceedance=exc,
                                  projections=None, econ=ctx["econ"], cpa_ctx=ctx["cpa_ctx"],
                                  econ_flags=ctx["econ_flags"], day_counts=ctx["day_counts"],
                                  current_period=current_period))

    # Rank: severity → exceedance → recency; deterministic tie-break.
    findings.sort(key=lambda f: (
        SEVERITY_RANK[f["risk_level"]],
        -f["supporting_metrics"]["exceedance"],
        f["period"] < current_period, f["period"],
        f"{f['entity']}|{f['region']}|{f['segment']}", f["alert_type"]))
    for i, f in enumerate(findings, start=1):
        f["finding_id"] = f"F-{i:03d}"

    logger.info("findings_builder: %d findings from %d flagged assessments; by level=%s",
                len(findings), len(flagged),
                pd.Series([f["risk_level"] for f in findings]).value_counts().to_dict())
    return findings


def compute_findings(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict:
    """Run the full chain; return the feed (findings) + browse layer (assessments)."""
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames
    from app.analytics.gl_processor import process_gl
    from app.analytics.metrics_calculator import calculate_metrics
    from app.analytics.projection_engine import project_fallout, project_volume
    from app.analytics.risk_classifier import classify

    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    period_close_day = int(cfg["period_close_day"])
    warn = float(cfg["cpa_ltv_warning_threshold"])
    data = load_data(config_path)
    merged = merge_frames(data)
    gl_states = process_gl(merged["gl_acquisition"], config_path)
    metrics = calculate_metrics(merged, gl_states, data["cogs_config"], data["retention_config"],
                                snapshot_date=snapshot_date, period_close_day=period_close_day)
    projection = {**project_volume(data, merged, metrics, snapshot_date=snapshot_date,
                                   pro_rate=str(cfg.get("pro_rate_default", "calendar_days"))),
                  **project_fallout(data, metrics, snapshot_date=snapshot_date)}
    assessments = classify(metrics, projection, gl_states, data["reference_data"],
                           snapshot_date=snapshot_date, thresholds=cfg["thresholds"],
                           cpa_ltv_warning_threshold=warn)["assessments"]
    findings = build_findings(assessments, metrics, projection, snapshot_date=snapshot_date,
                              thresholds=cfg["thresholds"], cpa_ltv_warning_threshold=warn)
    return {"findings": findings, "assessments": assessments}


# ===========================================================================
# 5. CLI — manual verification aid
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    findings = compute_findings()["findings"]
    print(f"\n=== ranked feed ({len(findings)} findings) ===")
    for f in findings:
        v = f["variance_pct"]
        vstr = f"{v:+.1f}%" if v is not None and pd.notna(v) else "  —  "
        exc = f["supporting_metrics"]["exceedance"]
        print(f"  {f['finding_id']}  {f['risk_level']:<6} {f['alert_type']:<20} "
              f"{f['entity']}/{f['region']}/{f['segment']:<20} {f['period']}  {vstr:>8}  "
              f"x{exc:<5} est={str(f['estimated']):<5} leaves={f['supporting_metrics']['leaf_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
