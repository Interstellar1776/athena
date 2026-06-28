#!/usr/bin/env python3
"""report_generator.py — assemble the validated feed for the UI / export.

Build Sequence §19 step 6 (context doc §15). This is the last hop before display: the analytics core
ranked the findings, the narrative layer filled them with Python-owned numbers, and this module
*structures* the result. It adds no analytics, recomputes no intermediates, and talks to no LLM — it
only partitions, summarizes, and renders (the decisions-log rule that ``report_generator`` must not
re-derive what the engine already produced).

Two sections, mirroring how the feed reads:

* **Actionable** — HIGH/MEDIUM findings, each carrying its narrative. These are what the operator acts
  on. Order is the analytics core's ranking (severity → exceedance → recency), preserved as-is — the
  feed arrives already ranked with ``finding_id`` assigned, so we never re-sort it here.
* **Low-priority / informational** — INFO/LOW findings as deterministic data blocks (no LLM
  narrative). The §6 "calm May-1, doesn't cry wolf" beat realized at the display layer: these sink to
  their own section the UI renders at the bottom, never suppressed (§9), just de-emphasized.

The summary block carries the counts a feed header shows plus the narrative-layer health (how many
narratives validated, how many fell back to the honest facts line — §17).
"""

from __future__ import annotations

import datetime as dt
import logging

logger = logging.getLogger(__name__)

# Severity tiers. The analytics feed emits HIGH/MEDIUM/INFO (and, defensively, LOW if a future alert
# adds one); HIGH/MEDIUM are narrated, INFO/LOW are the low-priority data-block tier.
NARRATED_LEVELS = ("HIGH", "MEDIUM")
LOW_PRIORITY_LEVELS = ("LOW", "INFO")
FALLBACK_FLAG = "fallback_after_retries"


# ===========================================================================
# 1. Build the structured report (pure assembly — no recompute, no LLM)
# ===========================================================================
def build_report(findings: list[dict], *, summary: dict, snapshot_date: dt.date,
                 current_period: str) -> dict:
    """Partition the processed feed into the two display sections + a summary.

    ``findings`` are the orchestrator's output — each already carries ``narrative_filled`` /
    ``validated`` / ``validation_flags`` (HIGH/MEDIUM from the retry loop, INFO/LOW as data blocks).
    Order is preserved within each section (the analytics ranking is authoritative).
    """
    actionable = [f for f in findings if f.get("risk_level") in NARRATED_LEVELS]
    low_priority = [f for f in findings if f.get("risk_level") in LOW_PRIORITY_LEVELS]

    # Per-severity counts over the whole feed (what a feed header shows).
    by_level: dict[str, int] = {}
    for f in findings:
        level = f.get("risk_level", "?")
        by_level[level] = by_level.get(level, 0) + 1

    # Narrative-layer health — measured over the narrated set only (INFO/LOW never call the LLM).
    n_validated = sum(1 for f in actionable if f.get("validated"))
    n_fallback = sum(1 for f in actionable if FALLBACK_FLAG in (f.get("validation_flags") or []))

    report_summary = {
        "snapshot_date": snapshot_date.isoformat(),
        "current_period": current_period,
        "n_findings": len(findings),
        "n_actionable": len(actionable),
        "n_low_priority": len(low_priority),
        "by_risk_level": by_level,
        "n_validated": n_validated,
        "n_fallback": n_fallback,
        # Pass rate over the narrated set; None when nothing was narrated (avoid 0/0).
        "validation_rate": round(n_validated / len(actionable), 3) if actionable else None,
        # Carry the analytics summary through for drill-down / debugging.
        "analytics": summary,
    }

    logger.info("report_generator: %d actionable, %d low-priority — %d/%d narratives valid (%d fallback)",
                len(actionable), len(low_priority), n_validated, len(actionable), n_fallback)

    return {
        "snapshot_date": snapshot_date.isoformat(),
        "current_period": current_period,
        "summary": report_summary,
        "actionable": actionable,
        "low_priority": low_priority,
    }


# ===========================================================================
# 2. Markdown render — the artifact we walk the demo arc with
# ===========================================================================
def _finding_md(f: dict) -> str:
    """One actionable finding: heading (status + alert + where) + raw placeholder-prose + filled prose.

    Same shape as ``narrative_validator``'s renderer so the two demonstrable artifacts read alike."""
    status = "✓ valid" if f.get("validated") else "✗ FLAGGED"
    flags = ("\n\n**Flags:** " + ", ".join(f["validation_flags"])) if f.get("validation_flags") else ""
    quoted = "\n".join("> " + line for line in (f.get("narrative_filled") or "").splitlines())
    return (f"### {f['finding_id']} · {status} · {f['alert_type']} · "
            f"{f['entity']}/{f['region']}/{f['segment']}{flags}\n\n"
            f"**Raw (placeholder-prose):**\n\n```\n{f.get('narrative', '')}\n```\n\n"
            f"**Filled:**\n\n{quoted}\n")


def _data_block_md(f: dict) -> str:
    """One low-priority finding as a compact data line (no narrative, just the Python-owned facts)."""
    return f"- **{f['finding_id']}** · {f['risk_level']} · {f['alert_type']} — {f.get('narrative_filled', '')}"


def _summary_md(s: dict) -> str:
    """The header block: severity mix + narrative-layer health."""
    levels = " · ".join(f"{lvl}: {n}" for lvl, n in s["by_risk_level"].items()) or "calm (empty feed)"
    rate = f"{s['validation_rate'] * 100:.0f}%" if s["validation_rate"] is not None else "—"
    return (f"- **Period:** {s['current_period']}  ·  **Snapshot:** {s['snapshot_date']}\n"
            f"- **Findings:** {s['n_findings']}  ({levels})\n"
            f"- **Actionable (narrated):** {s['n_actionable']}  ·  "
            f"**Low-priority:** {s['n_low_priority']}\n"
            f"- **Narratives valid:** {s['n_validated']}/{s['n_actionable']} ({rate})  ·  "
            f"**Fell back to facts line:** {s['n_fallback']}\n")


def render_markdown(report: dict) -> str:
    """Render the structured report as a single markdown document (open it in VSCode's preview)."""
    s = report["summary"]
    parts = [f"# Athena proactive feed — {report['current_period']}", "", _summary_md(s)]

    parts.append("\n---\n\n## Actionable (HIGH / MEDIUM)\n")
    if report["actionable"]:
        parts.extend(_finding_md(f) for f in report["actionable"])
    else:
        parts.append("_No actionable findings — the period is on track._\n")

    if report["low_priority"]:
        parts.append("\n---\n\n## Low-priority / informational (INFO / LOW)\n")
        parts.extend(_data_block_md(f) for f in report["low_priority"])

    return "\n".join(parts)
