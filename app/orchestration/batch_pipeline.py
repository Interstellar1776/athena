#!/usr/bin/env python3
"""batch_pipeline.py — the scheduled proactive run (the single batch entry point).

Build Sequence §19 step 6 (context doc §15; three-actor design in ``docs/the_hallucination_guard.md``
§6). This is the orchestrator: it threads the analytics core, the narrative generator, and the
narrative validator into one end-to-end proactive run, and it owns the one piece of logic that
belongs to neither single-responsibility module — the generate → validate → **regenerate** retry
loop. The §15 flow it realizes:

    run_pipeline (analytics → ranked §14 findings)
      → context_retriever (step 7 — empty for now; findings carry retrieved_context="")
      → narrate_findings (HIGH/MEDIUM: generate → validate → retry → honest fallback;
                          INFO/LOW: deterministic data block, no LLM)
      → build_report (assemble the two display sections + summary)
      → outputs/<snapshot_date>/report.{json,md}

Locked behaviors honored:

* **The LLM never types a number** (§4) — the generator emits placeholder-prose; the validator fills
  it; even the honest fallback line renders its numbers through ``render_placeholder``, so no raw
  digit ever originates here.
* **Never publish an untrusted narrative** (§17) — after ``max_attempts`` flagged tries, the finding
  falls back to a Python-built facts line (clearly marked) and keeps the last raw prose for audit.
* **Fail loud with stage context** (§17) — each stage is wrapped so a failure names the stage.
* **LLM config is env-driven** (§16) — the provider/model come from ``call_llm``; the prompt knobs
  come from ``system_config.yaml`` (overridable per run).

CLI / VSCode play button:
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.orchestration.batch_pipeline --out
    #   --all   walk every snapshot (the demo arc)   ·   --limit N   cap the narrated set
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from app.analytics.variance_engine import PipelineError, run_pipeline
from app.llm.llm_client import call_llm
from app.llm.narrative_generator import (
    DEFAULT_AUDIENCE,
    DEFAULT_NUMBERS,
    DEFAULT_STYLE,
    generate_narratives,
)
from app.llm.placeholders import available_placeholders, render_placeholder
from app.reporting.report_generator import (
    NARRATED_LEVELS,
    build_report,
    render_markdown,
)
from app.validation.narrative_validator import validate_narrative

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

DEFAULT_MAX_ATTEMPTS = 3            # 1 initial + 2 regenerations before the honest fallback
FALLBACK_FLAG = "fallback_after_retries"


# ===========================================================================
# 1. Stage runner — fail loud with stage context (§17), mirroring variance_engine
# ===========================================================================
def _stage(name: str, fn: Callable, *args, **kwargs) -> Any:
    """Run one pipeline stage; re-raise any failure as ``PipelineError`` naming the stage (§17)."""
    try:
        return fn(*args, **kwargs)
    except PipelineError:
        raise                                                 # already carries stage context
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with context
        raise PipelineError(f"batch_pipeline: stage '{name}' failed: {exc}") from exc


# ===========================================================================
# 2. Number-safe one-liners — Python owns every digit (§4), even in the fallback
# ===========================================================================
def _headline_metrics(finding: dict) -> str:
    """The finding's headline numbers, each rendered by Python (never a raw digit). Only the
    placeholders actually present on this finding are included (so a trend finding with no variance
    simply omits it)."""
    legal = set(available_placeholders(finding))
    bits = []
    if "actual" in legal:
        bits.append(f"actual {render_placeholder('actual', finding)}")
    if "reference" in legal:
        bits.append(f"plan {render_placeholder('reference', finding)}")
    if "variance_pct" in legal:
        bits.append(f"variance {render_placeholder('variance_pct', finding)}")
    return ", ".join(bits)


def _describe(finding: dict) -> str:
    """A deterministic, number-safe sentence summarizing one finding — the shared core of the honest
    fallback line and the INFO/LOW data block."""
    est = ", estimated" if finding.get("estimated") else ""
    where = f"{finding.get('segment')} in {finding.get('entity')} {finding.get('region')}"
    metrics = _headline_metrics(finding)
    tail = f": {metrics}." if metrics else "."
    return f"{finding.get('metric')} in {where} ({finding.get('risk_level')}{est}){tail}"


def _facts_line(finding: dict) -> str:
    """The honest fallback when a narrative can't pass the validator after N tries (§17) — clearly
    marked unverified so it is never mistaken for a trusted narrative."""
    return "⚠ UNVERIFIED NARRATIVE — " + _describe(finding)


def _data_block(finding: dict) -> str:
    """The INFO/LOW low-priority line — a legitimate data block (no LLM, no 'unverified' framing)."""
    return _describe(finding)


# ===========================================================================
# 3. The retry loop — generate → validate → regenerate → honest fallback
# ===========================================================================
def _narrate_one(finding: dict, *, client: Callable[..., str], knobs: dict,
                 max_attempts: int) -> dict:
    """Narrate one HIGH/MEDIUM finding, retrying on a validation flag and falling back honestly.

    Returns a new dict with ``narrative`` (raw, auditable), ``narrative_filled`` (display),
    ``validated``, and ``validation_flags`` populated.
    """
    last_flags: list[str] = []
    last_raw = ""
    for attempt in range(1, max_attempts + 1):
        raw = generate_narratives([finding], client=client, **knobs)[0]["narrative"]
        res = validate_narrative(raw, finding)
        if res.ok:
            return {**finding, "narrative": raw, "narrative_filled": res.filled,
                    "validated": True, "validation_flags": []}
        last_flags, last_raw = res.flags, raw
        logger.info("batch_pipeline: %s narrative flagged on attempt %d/%d (%s)",
                    finding.get("finding_id"), attempt, max_attempts, res.flags)

    # Exhausted — never publish the untrusted prose; emit the honest, marked facts line (§17).
    logger.warning("batch_pipeline: %s fell back to facts line after %d attempts",
                   finding.get("finding_id"), max_attempts)
    return {**finding, "narrative": last_raw, "narrative_filled": _facts_line(finding),
            "validated": False, "validation_flags": last_flags + [FALLBACK_FLAG]}


def narrate_findings(findings: list[dict], *, client: Callable[..., str], knobs: dict,
                     max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[dict]:
    """Attach a display narrative to every finding.

    HIGH/MEDIUM run the retry loop (one LLM call per attempt). INFO/LOW skip the LLM entirely and get
    a deterministic data block — so a noisy early snapshot full of low-priority findings costs nothing
    to render and the feed reads calm (§6).
    """
    out: list[dict] = []
    for finding in findings:
        if finding.get("risk_level") in NARRATED_LEVELS:
            out.append(_narrate_one(finding, client=client, knobs=knobs, max_attempts=max_attempts))
        else:
            out.append({**finding, "narrative": "", "narrative_filled": _data_block(finding),
                        "validated": True, "validation_flags": []})
    n_narrated = sum(1 for f in findings if f.get("risk_level") in NARRATED_LEVELS)
    logger.info("batch_pipeline: narrated %d/%d findings (%d low-priority data blocks)",
                n_narrated, len(out), len(out) - n_narrated)
    return out


# ===========================================================================
# 4. Knobs + JSON-safe export
# ===========================================================================
def _load_knobs(cfg: dict) -> dict:
    """Resolve the three prompt knobs from the optional ``narrative:`` block, falling back to the
    generator's defaults — the deployed run is config-driven, like data_mode/snapshot (§8/§16)."""
    nb = cfg.get("narrative") or {}
    return {"numbers": nb.get("numbers", DEFAULT_NUMBERS),
            "style": nb.get("style", DEFAULT_STYLE),
            "audience": nb.get("audience", DEFAULT_AUDIENCE)}


def _json_safe(obj: Any) -> Any:
    """Make a report dict JSON-serializable: numpy scalars → native, NaN/NaT → null, dates → ISO.
    Findings carry pandas/numpy values, so a naive ``json.dumps`` would fail (why the dev dump used
    CSVs); this sanitizes recursively. ``json.dump(default=str)`` is the backstop for anything exotic."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, np.generic):                           # numpy int64/float64/bool_ → python scalar
        obj = obj.item()
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def _write_outputs(report: dict) -> Path:
    """Write ``report.json`` (UI/export) and ``report.md`` (the demo-arc artifact) under
    ``outputs/<snapshot_date>/`` — keyed by snapshot date so the ``--all`` arc walk never collides."""
    out_dir = REPO_ROOT / "outputs" / report["snapshot_date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(_json_safe(report), indent=2, default=str))
    (out_dir / "report.md").write_text(render_markdown(report))
    return out_dir


# ===========================================================================
# 5. The batch run — the single proactive entry point
# ===========================================================================
def run_batch(config_path: Path = DEFAULT_SYSTEM_CONFIG, *, client: Callable[..., str] = call_llm,
              limit: int | None = None, knobs_override: dict | None = None,
              write: bool = True) -> dict:
    """Run the full proactive pipeline for the configured snapshot and return the structured report.

    Bad data halts loudly inside ``run_pipeline``'s ingestion gate before any narration (§17). An
    empty feed is not an error — it yields a valid empty report with zero LLM calls.
    """
    cfg = yaml.safe_load(Path(config_path).read_text())
    knobs = knobs_override or _load_knobs(cfg)

    result = _stage("analytics", run_pipeline, config_path)
    findings = result["findings"]
    if limit is not None:
        findings = findings[:limit]

    # context_retriever (step 7) attaches operational notes here; until then retrieved_context="".
    processed = _stage("narrate", narrate_findings, findings, client=client, knobs=knobs)
    report = _stage("report", build_report, processed, summary=result["summary"],
                    snapshot_date=result["snapshot_date"], current_period=result["current_period"])

    if write:
        out_dir = _write_outputs(report)
        logger.info("batch_pipeline: wrote report to %s", out_dir)
    return report


def run_arc(config_path: Path = DEFAULT_SYSTEM_CONFIG, *, client: Callable[..., str] = call_llm,
            dates: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Walk every snapshot (the demo arc): run the batch once per snapshot date.

    Drives multiple snapshots by writing a temp config with ``snapshot_date`` swapped — non-invasive,
    so ``variance_engine``/``data_loader`` stay untouched. The temp config is written *into the real
    config directory* so the data loader still finds the sibling config tables (it keys them off the
    config file's parent dir).
    """
    cfg_path = Path(config_path)
    cfg = yaml.safe_load(cfg_path.read_text())

    if dates is None:
        sp = Path(cfg["snapshot_path"])
        snap_root = sp if sp.is_absolute() else REPO_ROOT / sp
        dates = sorted(d.name for d in snap_root.iterdir() if d.is_dir())

    reports = []
    for date in dates:
        logger.info("batch_pipeline: --- arc snapshot %s ---", date)
        fd, tmp_name = tempfile.mkstemp(suffix=".yaml", prefix=".batch_arc_", dir=str(cfg_path.parent))
        os.close(fd)                                          # we only need the path; write via Path
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(yaml.safe_dump({**cfg, "snapshot_date": date}))
            reports.append(run_batch(tmp_path, client=client, limit=limit))
        finally:
            tmp_path.unlink(missing_ok=True)
    return reports


# ===========================================================================
# 6. CLI — the one-click VSCode play-button entry
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Athena proactive batch run: analytics → narrate → report.")
    ap.add_argument("--all", action="store_true", help="walk every snapshot (the demo arc)")
    ap.add_argument("--limit", type=int, default=None, help="cap how many top-ranked findings are narrated")
    ap.add_argument("--numbers", choices=["withhold", "show"], default=None, help="override the config knob")
    ap.add_argument("--style", choices=["paragraph", "bullets"], default=None, help="override the config knob")
    ap.add_argument("--audience", choices=["exec", "analyst"], default=None, help="override the config knob")
    ap.add_argument("--out", action="store_true",
                    help="write report.json + report.md (the default); accepted for explicitness")
    args = ap.parse_args()

    # CLI knob overrides win over the config block; unset flags leave the config value in place.
    overrides = {k: v for k, v in (("numbers", args.numbers), ("style", args.style),
                                   ("audience", args.audience)) if v is not None}
    cfg = yaml.safe_load(DEFAULT_SYSTEM_CONFIG.read_text())
    knobs = {**_load_knobs(cfg), **overrides}

    reports = (run_arc(client=call_llm, limit=args.limit) if args.all
               else [run_batch(client=call_llm, limit=args.limit, knobs_override=knobs)])

    for report in reports:
        s = report["summary"]
        print(f"\n=== {report['current_period']} (snapshot {report['snapshot_date']}) ===")
        print(f"  findings: {s['n_findings']}  ({s['by_risk_level'] or 'calm (empty feed)'})")
        print(f"  actionable: {s['n_actionable']}  ·  low-priority: {s['n_low_priority']}")
        rate = f"{s['validation_rate'] * 100:.0f}%" if s["validation_rate"] is not None else "—"
        print(f"  narratives valid: {s['n_validated']}/{s['n_actionable']} ({rate})  ·  "
              f"fell back: {s['n_fallback']}")
        print(f"  → outputs/{report['snapshot_date']}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
