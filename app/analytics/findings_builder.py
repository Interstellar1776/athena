#!/usr/bin/env python3
"""findings_builder.py — assemble the §14 structured findings from the assessment table.

Build Sequence §19 step 3 (context doc §15), after ``risk_classifier``. This is the bridge from the
flat **assessment table** (every metric × period × grain, scored) to the **structured findings** that
are the system contract (§14) every downstream module reads (context retrieval → narrative → report).
It is **deterministic — no LLM**.

What it does (decisions in ``docs/decisions_log.md`` — BS3 analytics core, module 4):

* **Non-LOW become findings.** HIGH/MEDIUM/INFO assessments are turned into §14 findings; the full
  assessment table travels alongside as the **browse / drill-down layer** ("see everything" lives
  there, not as a heavy finding per on-track metric).
* **Roll up to one finding per (alert_type, unit, period).** Leaf-grain alerts (COGS / margin /
  fallout / volume) roll leaf→unit — max severity, the *worst leaf* as the headline — with every
  leaf nested under ``supporting_metrics.leaves`` for the double-click. (The COGS anomaly is one
  finding holding its 3 leaves.)
* **Bundle the unit's economic context** (§14 [LOCKED]): COGS/LTV/margin + method labels,
  ``unit_economics_flag``, ``gl_completeness_state``, volume projections, ``frozen_reference`` /
  ``restatement_delta``, and ``supporting_metrics``.
* **Rank for the feed** across the 6-month window: severity (HIGH>MEDIUM>INFO) → magnitude → recency;
  ``finding_id`` ``F-001…`` assigned in ranked order.

CLI:
    python -m app.analytics.findings_builder
    # run the chain; print the ranked feed + count by risk level.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
import yaml

from app.analytics.data_merger import DIMS
from app.analytics.gl_processor import UNIT
from app.analytics.metrics_calculator import _snapshot_period
from app.analytics.risk_classifier import HIGH, INFO, LOW, MEDIUM  # noqa: F401

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

FLAGGED = {HIGH, MEDIUM, INFO}                       # what becomes a finding (LOW stays in the table)
SEVERITY_RANK = {HIGH: 0, MEDIUM: 1, INFO: 2, LOW: 3}
LEAF_ONLY = [c for c in DIMS if c not in UNIT]
VOLUME_ALERTS = {"volume_miss_linear", "volume_miss_weighted"}


# ===========================================================================
# 1. Context lookups — built once, keyed by (unit, period)
# ===========================================================================
def _unit_economics(economics: pd.DataFrame) -> dict:
    """The unit's economic context (COGS/LTV/margin + methods), rolled from leaves (mean — uniform
    within a unit), keyed (entity, region, segment, period)."""
    g = economics.groupby(UNIT + ["period"], observed=True)
    rolled = g.agg(cogs_per_unit=("cogs_per_unit", "mean"), cogs_method=("cogs_method", "first"),
                   ltv=("ltv", "mean"), ltv_method=("ltv_method", "first"),
                   margin_per_unit=("margin_per_unit", "mean"),
                   margin_method=("margin_method", "first")).reset_index()
    return {tuple(r[c] for c in UNIT + ["period"]): r for _, r in rolled.iterrows()}


def _unit_cpa(cpa: pd.DataFrame) -> dict:
    """GL state + CPA bases per (unit, period), for context on every finding of that unit."""
    return {tuple(r[c] for c in UNIT + ["period"]): r for _, r in cpa.iterrows()}


def _unit_economics_flags(assessments: pd.DataFrame) -> set:
    """(unit, period) where CPA-vs-LTV inversion crossed (HIGH) — the §14 unit_economics_flag."""
    inv = assessments[(assessments["alert_type"] == "cpa_ltv_inversion") &
                      (assessments["risk_level"] == HIGH)]
    return {tuple(r[c] for c in UNIT + ["period"]) for _, r in inv.iterrows()}


def _volume_projection_by_unit(volume_proj: pd.DataFrame) -> dict:
    """Period-end volume projections summed leaf→unit, keyed (unit, period) — for volume findings."""
    if volume_proj is None or volume_proj.empty:
        return {}
    g = (volume_proj.groupby(UNIT + ["period"], observed=True)
         .agg(linear=("converted_proj_linear", "sum"),
              weighted=("converted_proj_weighted", "sum")).reset_index())
    return {tuple(r[c] for c in UNIT + ["period"]): r for _, r in g.iterrows()}


def _day_counts(volume_proj: pd.DataFrame, current_period: str) -> dict:
    """period → (days_elapsed, days_in_period). Current period from the projection (partial); any
    other (closed) period is fully elapsed."""
    out = {}
    if volume_proj is not None and len(volume_proj):
        row = volume_proj.iloc[0]
        out[current_period] = (int(row["days_elapsed"]), int(row["days_in_period"]))
    return out


def _days_for(period: str, day_counts: dict) -> tuple[int, int]:
    if period in day_counts:
        return day_counts[period]
    n = pd.Period(period, freq="M").days_in_month        # closed period → fully elapsed
    return n, n


# ===========================================================================
# 2. Roll a group of leaf rows into one finding's headline + leaf breakdown
# ===========================================================================
def _magnitude(row) -> float:
    v = row.get("variance_pct")
    return abs(v) if pd.notna(v) else 0.0


def _roll_group(group: pd.DataFrame) -> tuple[pd.Series, list[dict]]:
    """Headline = the worst leaf (max unfavorable magnitude, breaking ties on max severity); the
    nested leaf breakdown carries every leaf for drill-down."""
    g = group.copy()
    g["_sev"] = g["risk_level"].map(SEVERITY_RANK)
    g["_mag"] = g.apply(_magnitude, axis=1)
    headline = g.sort_values(["_sev", "_mag"], ascending=[True, False]).iloc[0]
    leaves = [{**{c: r[c] for c in DIMS}, "actual": r["actual"], "variance_pct": r["variance_pct"],
               "risk_level": r["risk_level"], "supporting": r["supporting"]}
              for _, r in group.iterrows()]
    return headline, leaves


# ===========================================================================
# 3. Assemble one §14 finding
# ===========================================================================
def _assemble(headline: pd.Series, leaves: list[dict], *, econ: dict, cpa_ctx: dict,
              econ_flags: set, vol_proj: dict, day_counts: dict, current_period: str) -> dict:
    period = headline["period"]
    unit_key = (headline["entity"], headline["region"], headline["segment"], period)
    alert_type = headline["alert_type"]
    days_elapsed, days_in = _days_for(period, day_counts)

    e = econ.get(unit_key)
    c = cpa_ctx.get(unit_key)
    is_volume = alert_type in VOLUME_ALERTS
    vp = vol_proj.get(unit_key) if is_volume else None
    is_restatement = alert_type == "restatement"

    finding = {
        "finding_id": None,                                  # assigned after ranking
        "entity": headline["entity"], "region": headline["region"], "segment": headline["segment"],
        # leaf-only dims stay blank on a rolled unit finding — the spread lives in leaves.
        **{c2: "" for c2 in LEAF_ONLY},
        "metric": headline["metric"],
        "alert_type": alert_type,
        "grain": headline["grain"],
        "period": period,
        "days_elapsed": days_elapsed,
        "days_in_period": days_in,
        "confidence": headline["confidence"],
        "is_projectable": period == current_period,

        "actual": headline["actual"],
        "actual_method": headline["actual_method"],
        "reference_value": headline["reference_value"],
        "reference_type": headline["reference_type"],
        "variance_pct": headline["variance_pct"],
        "variance_direction": headline["variance_direction"],
        "risk_level": headline["risk_level"],
        "estimated": bool(headline["estimated"]),

        "projected_period_end_linear": round(vp["linear"], 2) if vp is not None else None,
        "projected_period_end_weighted": round(vp["weighted"], 2) if vp is not None else None,

        "cogs_per_unit": round(e["cogs_per_unit"], 4) if e is not None else None,
        "cogs_method": e["cogs_method"] if e is not None else None,
        "ltv": round(e["ltv"], 2) if e is not None and pd.notna(e["ltv"]) else None,
        "ltv_method": e["ltv_method"] if e is not None else None,
        "margin_per_unit": round(e["margin_per_unit"], 2) if e is not None else None,
        "margin_method": e["margin_method"] if e is not None else None,
        "unit_economics_flag": unit_key in econ_flags,

        "gl_completeness_state": c["gl_completeness_state"] if c is not None else None,
        "frozen_reference": (round(headline["reference_value"], 2) if is_restatement else None),
        "restatement_delta": (round(headline["actual"] - headline["reference_value"], 2)
                              if is_restatement else None),

        "supporting_metrics": {**(headline["supporting"] or {}),
                               "grain": headline["grain"], "leaf_count": len(leaves),
                               "leaves": leaves},

        # populated downstream
        "retrieved_context": "",
        "narrative": "",
        "validated": False,
        "validation_flags": [],
    }
    return finding


# ===========================================================================
# 4. Public entry points
# ===========================================================================
def build_findings(assessments: pd.DataFrame, metrics: dict[str, pd.DataFrame],
                   projection: dict[str, pd.DataFrame], *, snapshot_date: dt.date) -> list[dict]:
    """Select non-LOW assessments, roll them into §14 findings, rank, and assign ids."""
    current_period = _snapshot_period(snapshot_date)
    econ = _unit_economics(metrics["economics"])
    cpa_ctx = _unit_cpa(metrics["cpa"])
    econ_flags = _unit_economics_flags(assessments)
    vol_proj = _volume_projection_by_unit(projection.get("volume_projection"))
    day_counts = _day_counts(projection.get("volume_projection"), current_period)

    flagged = assessments[assessments["risk_level"].isin(FLAGGED)]
    findings = []
    for _, group in flagged.groupby(["alert_type", "group_key", "period"], sort=False):
        headline, leaves = _roll_group(group)
        findings.append(_assemble(headline, leaves, econ=econ, cpa_ctx=cpa_ctx,
                                  econ_flags=econ_flags, vol_proj=vol_proj,
                                  day_counts=day_counts, current_period=current_period))

    # Rank: severity → magnitude → recency; deterministic tie-break on (group_key, alert_type).
    findings.sort(key=lambda f: (
        SEVERITY_RANK[f["risk_level"]],
        -abs(f["variance_pct"]) if f["variance_pct"] is not None and pd.notna(f["variance_pct"]) else 0.0,
        f["period"] < current_period,                        # current period first
        f["period"],
        f"{f['entity']}|{f['region']}|{f['segment']}", f["alert_type"]))
    for i, f in enumerate(findings, start=1):
        f["finding_id"] = f"F-{i:03d}"

    logger.info("findings_builder: %d findings from %d flagged assessments; by level=%s",
                len(findings), len(flagged),
                pd.Series([f["risk_level"] for f in findings]).value_counts().to_dict())
    return findings


def compute_findings(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict:
    """Convenience entry: run the full chain; return the feed (findings) + browse layer (assessments)."""
    from app.analytics.data_loader import load_data
    from app.analytics.data_merger import merge_frames
    from app.analytics.gl_processor import process_gl
    from app.analytics.metrics_calculator import calculate_metrics
    from app.analytics.projection_engine import project_fallout, project_volume
    from app.analytics.risk_classifier import classify

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
    assessments = classify(metrics, projection, gl_states, data["reference_data"],
                           snapshot_date=snapshot_date, thresholds=cfg["thresholds"],
                           cpa_ltv_warning_threshold=float(cfg["cpa_ltv_warning_threshold"]))["assessments"]
    findings = build_findings(assessments, metrics, projection, snapshot_date=snapshot_date)
    return {"findings": findings, "assessments": assessments}


# ===========================================================================
# 5. CLI — manual verification aid
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    findings = compute_findings()["findings"]
    print(f"\n=== ranked feed ({len(findings)} findings) ===")
    for f in findings:
        leaves = f["supporting_metrics"]["leaf_count"]
        v = f["variance_pct"]
        vstr = f"{v:+.1f}%" if v is not None and pd.notna(v) else "  —  "
        print(f"  {f['finding_id']}  {f['risk_level']:<6} {f['alert_type']:<20} "
              f"{f['entity']}/{f['region']}/{f['segment']:<20} {f['period']}  "
              f"{vstr:>8}  est={str(f['estimated']):<5} leaves={leaves}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
