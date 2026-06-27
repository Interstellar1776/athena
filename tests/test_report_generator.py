"""Tests for the report generator (Build Sequence §19 step 6).

Pins the display contract: the processed feed is partitioned into an actionable section (HIGH/MEDIUM,
order preserved) and a low-priority section (INFO/LOW), the summary counts the narrative-layer health
(validated vs fell-back), and the markdown carries both. Hermetic — synthetic findings, no pipeline,
no LLM.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.reporting import report_generator as rg  # noqa: E402


def _processed(finding_id: str, level: str, *, validated: bool = True,
               flags: list[str] | None = None, filled: str = "filled prose") -> dict:
    """A finding as it looks AFTER the orchestrator (narrative_filled / validated / flags set)."""
    return {
        "finding_id": finding_id, "entity": "ERCOT", "region": "North", "segment": "Door_to_Door",
        "metric": "cost_per_acquisition", "alert_type": "cpa_spike", "risk_level": level,
        "estimated": True, "narrative": "raw {variance_pct}", "narrative_filled": filled,
        "validated": validated, "validation_flags": flags or [],
    }


def _feed() -> list[dict]:
    # Order as the analytics core would emit (severity → exceedance); report must preserve it.
    return [
        _processed("F-001", "HIGH", validated=True),
        _processed("F-002", "MEDIUM", validated=False, flags=["stray_numeral:5", rg.FALLBACK_FLAG],
                   filled="⚠ UNVERIFIED NARRATIVE — cost_per_acquisition …"),
        _processed("F-003", "INFO", filled="cost_per_acquisition in Door_to_Door … (INFO)."),
    ]


def _build():
    return rg.build_report(_feed(), summary={"n_findings": 3},
                           snapshot_date=dt.date(2024, 5, 22), current_period="2024-05")


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------
def test_partitions_actionable_and_low_priority():
    report = _build()
    assert [f["finding_id"] for f in report["actionable"]] == ["F-001", "F-002"]   # HIGH, MEDIUM
    assert [f["finding_id"] for f in report["low_priority"]] == ["F-003"]          # INFO
    # Order within the actionable section is preserved (no re-sort).
    assert report["actionable"][0]["risk_level"] == "HIGH"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_summary_counts_and_validation_health():
    s = _build()["summary"]
    assert s["n_findings"] == 3
    assert s["n_actionable"] == 2 and s["n_low_priority"] == 1
    assert s["by_risk_level"] == {"HIGH": 1, "MEDIUM": 1, "INFO": 1}
    assert s["n_validated"] == 1                  # only F-001 validated
    assert s["n_fallback"] == 1                   # F-002 fell back
    assert s["validation_rate"] == 0.5            # 1 of 2 narrated
    assert s["snapshot_date"] == "2024-05-22" and s["current_period"] == "2024-05"


def test_validation_rate_is_none_when_nothing_narrated():
    report = rg.build_report([_processed("F-001", "INFO")], summary={},
                             snapshot_date=dt.date(2024, 5, 1), current_period="2024-05")
    assert report["summary"]["n_actionable"] == 0
    assert report["summary"]["validation_rate"] is None      # no 0/0 crash


def test_empty_feed_produces_a_valid_calm_report():
    report = rg.build_report([], summary={}, snapshot_date=dt.date(2024, 5, 1),
                             current_period="2024-05")
    assert report["actionable"] == [] and report["low_priority"] == []
    assert report["summary"]["by_risk_level"] == {}
    md = rg.render_markdown(report)
    assert "on track" in md                                   # the calm-feed message renders


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------
def test_markdown_contains_summary_actionable_and_low_priority():
    md = rg.render_markdown(_build())
    assert "# Athena proactive feed — 2024-05" in md
    assert "Actionable (HIGH / MEDIUM)" in md and "Low-priority / informational" in md
    assert "F-001" in md and "F-002" in md and "F-003" in md
    assert "✗ FLAGGED" in md                                  # F-002's fallback shows as flagged
    assert "Fell back to facts line:** 1" in md
