#!/usr/bin/env python3
"""variance_engine.py — the analytics-core orchestrator (single pass).

Build Sequence §19 step 3 (context doc §15). It threads the **pure cores once**, in order, and returns
the structured findings plus everything behind them. This realizes the §15 analytics flow end-to-end:

    data_loader (load + ingestion gate + clean)
      → variance_engine[ merge → gl_processor → metrics → projection(volume+fallout)
                         → risk_classifier → findings_builder ]
      → findings (the proactive feed)

The per-module ``compute_*`` wrappers each re-run the whole upstream chain (handy for module CLIs/tests).
``variance_engine`` is the **efficient production path**: each core runs exactly once and its output is
threaded forward, so ``gl_states`` / ``metrics`` / ``projection`` are computed a single time and reused by
both ``risk_classifier`` and ``findings_builder``. It is the single entry the proactive batch pipeline
(Phase 6) calls.

Design (decisions in ``docs/decisions_log.md`` — BS3 analytics core, orchestrator):

* **Rich result from one pass** — findings (feed) + assessments (browse) + the intermediate frames
  (gl_states, metrics, projection, merged) + a summary, so ``report_generator`` / the UI drill-down /
  debugging get everything without recomputing.
* **Fail loud with stage context (§17)** — each step is wrapped so a failure names the stage; the
  ``data_loader`` gate (which halts loudly on bad data) runs *before* ``run`` and propagates untouched.
* **An empty feed is not an error** — a calm snapshot returns ``findings=[]`` cleanly (never a crash).
* **Reuse, don't reimplement** — calls the existing pure functions; no analytics logic lives here.

CLI:
    python -m app.analytics.variance_engine
    # run the full analytics pipeline for the configured snapshot; print the feed + stage shapes.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

from app.analytics.data_merger import merge_frames
from app.analytics.findings_builder import build_findings
from app.analytics.gl_processor import gl_completeness
from app.analytics.metrics_calculator import _snapshot_period, calculate_metrics
from app.analytics.projection_engine import project_fallout, project_volume
from app.analytics.risk_classifier import classify

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"


class PipelineError(RuntimeError):
    """A stage of the analytics pipeline failed — carries which stage, for a loud, actionable halt."""


# ===========================================================================
# 1. Stage runner — run a core, log its shape, fail loud with stage context
# ===========================================================================
def _shape(result: Any) -> str:
    """Compact shape description for the log line (frames, dicts of frames, finding lists)."""
    if isinstance(result, pd.DataFrame):
        return f"{result.shape[0]}×{result.shape[1]}"
    if isinstance(result, dict):
        return "{" + ", ".join(f"{k}:{_shape(v)}" for k, v in result.items()) + "}"
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    return type(result).__name__


def _stage(name: str, fn: Callable, *args, **kwargs) -> Any:
    """Run one pipeline stage; log its output shape; re-raise any failure with the stage name (§17)."""
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with context
        raise PipelineError(f"variance_engine: stage '{name}' failed: {exc}") from exc
    logger.info("variance_engine: %-12s → %s", name, _shape(out))
    return out


# ===========================================================================
# 2. Core — thread the pure cores once over already-validated data
# ===========================================================================
def run(data: dict[str, pd.DataFrame], *, snapshot_date: dt.date, period_close_day: int,
        pro_rate: str, thresholds: dict, cpa_ltv_warning_threshold: float = 0.80) -> dict[str, Any]:
    """Run the analytics core once over ``data`` (already loaded + validated by ``data_loader``).

    Returns the rich result (see module docstring). No I/O, no re-validation — ``run_pipeline`` is the
    entry that owns loading + the ingestion gate.
    """
    merged = _stage("merge", merge_frames, data)
    gl_states = _stage("gl_processor", gl_completeness, merged["gl_acquisition"],
                       snapshot_date=snapshot_date, period_close_day=period_close_day)
    metrics = _stage("metrics", calculate_metrics, merged, gl_states,
                     data["cogs_config"], data["retention_config"],
                     snapshot_date=snapshot_date, period_close_day=period_close_day)
    projection = _stage("projection", _project, data, merged, metrics,
                        snapshot_date=snapshot_date, pro_rate=pro_rate)
    assessments = _stage("risk_classifier", classify, metrics, projection, gl_states,
                         data["reference_data"], snapshot_date=snapshot_date, thresholds=thresholds,
                         cpa_ltv_warning_threshold=cpa_ltv_warning_threshold)["assessments"]
    findings = _stage("findings_builder", build_findings, assessments, metrics, projection,
                      snapshot_date=snapshot_date, thresholds=thresholds,
                      cpa_ltv_warning_threshold=cpa_ltv_warning_threshold)

    by_level = pd.Series([f["risk_level"] for f in findings]).value_counts().to_dict() if findings else {}
    summary = {"n_findings": len(findings), "by_risk_level": by_level, "n_assessments": len(assessments)}
    logger.info("variance_engine: %d finding(s) for %s — %s",
                len(findings), _snapshot_period(snapshot_date), by_level or "calm (empty feed)")

    return {
        "snapshot_date": snapshot_date,
        "current_period": _snapshot_period(snapshot_date),
        "findings": findings,
        "assessments": assessments,
        "metrics": metrics,
        "projection": projection,
        "gl_states": gl_states,
        "merged": merged,
        "summary": summary,
    }


def _project(data, merged, metrics, *, snapshot_date, pro_rate) -> dict:
    """Both projection frames (volume + fallout) as one stage."""
    return {**project_volume(data, merged, metrics, snapshot_date=snapshot_date, pro_rate=pro_rate),
            **project_fallout(data, metrics, snapshot_date=snapshot_date)}


# ===========================================================================
# 3. Wrapper — load + gate + clean, then run (the single batch entry)
# ===========================================================================
def run_pipeline(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict[str, Any]:
    """Single entry: ``load_data`` (load + ingestion gate + clean) → ``run`` over the validated data.

    Bad data halts loudly inside ``load_data`` (the gate) before any analytics runs (§17)."""
    from app.analytics.data_loader import load_data

    cfg = yaml.safe_load(Path(config_path).read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    data = load_data(config_path)                            # load + gate + clean (halts on bad data)
    return run(data, snapshot_date=snapshot_date, period_close_day=int(cfg["period_close_day"]),
               pro_rate=str(cfg.get("pro_rate_default", "calendar_days")),
               thresholds=cfg["thresholds"],
               cpa_ltv_warning_threshold=float(cfg["cpa_ltv_warning_threshold"]))


# ===========================================================================
# 4. CLI — run the full analytics pipeline, print the feed + stage shapes
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_pipeline()
    s = result["summary"]
    print(f"\n=== analytics pipeline — {result['current_period']} "
          f"({s['n_findings']} findings, {s['n_assessments']} assessments) ===")
    print(f"by risk level: {s['by_risk_level'] or 'calm (empty feed)'}")
    for f in result["findings"][:12]:
        v = f["variance_pct"]
        vstr = f"{v:+.1f}%" if v is not None and pd.notna(v) else "  —  "
        print(f"  {f['finding_id']}  {f['risk_level']:<6} {f['alert_type']:<20} "
              f"{f['entity']}/{f['region']}/{f['segment']:<20} {f['period']}  {vstr:>8}  "
              f"est={f['estimated']}")
    if len(result["findings"]) > 12:
        print(f"  … and {len(result['findings']) - 12} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
