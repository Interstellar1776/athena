"""Tests for the query selector (Build Sequence §19 step 9).

Pins the pure select step: `explain` filters the flagged findings by the spec's scope+metric; `lookup`
reads the assessments frame so a CALM channel still answers (with its value + method label); an
unmatched scope returns empty. No LLM, no I/O — synthetic result dict.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics import query_selector as qs  # noqa: E402
from app.llm.query_router import QuerySpec  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402

SNAPSHOT = dt.date(2024, 5, 22)


def _assessments() -> pd.DataFrame:
    cols = ["entity", "region", "segment", "period", "alert_type", "metric", "actual", "actual_method",
            "reference_value", "reference_type", "variance_pct", "variance_direction", "risk_level",
            "estimated", "confidence", "supporting"]
    rows = [
        # the flagged CPA row (mirrors the finding)
        ("ERCOT", "North", "Door_to_Door", "2024-05", "cpa_spike", "cost_per_acquisition", 160.74,
         "gl_partial", 125.0, "plan", 28.59, "UNFAVORABLE", "HIGH", True, "low", {"cpa_t3m": 160.74}),
        # a CALM fallout row on a different channel — no alert, but still has a value
        ("PJM", "East", "Telemarketing", "2024-05", "fallout_rate", "fallout_rate", 0.11,
         "real", 0.12, "plan", -8.3, "FAVORABLE", "LOW", False, "high", {"submissions": 200}),
    ]
    return pd.DataFrame(rows, columns=cols)


def _result() -> dict:
    return {"findings": [{**cpa_finding()}], "assessments": _assessments(), "snapshot_date": SNAPSHOT}


def _spec(**over) -> QuerySpec:
    base = dict(question="q", intent="explain", metric=None, entity=None, region=None, segment=None)
    return QuerySpec(**{**base, **over})


# ---------------------------------------------------------------------------
# explain — from the flagged findings
# ---------------------------------------------------------------------------
def test_explain_selects_the_matching_finding():
    sel = qs.select(_spec(intent="explain", metric="cost_per_acquisition", entity="ERCOT",
                          region="North", segment="Door_to_Door"), _result())
    assert sel.kind == "explain" and len(sel.records) == 1
    assert sel.records[0]["metric"] == "cost_per_acquisition"


def test_explain_no_match_is_empty():
    sel = qs.select(_spec(intent="explain", metric="cost_per_acquisition", entity="PJM"), _result())
    assert sel.records == []


# ---------------------------------------------------------------------------
# lookup — from assessments, so a calm channel still answers
# ---------------------------------------------------------------------------
def test_lookup_calm_channel_returns_value_and_method():
    sel = qs.select(_spec(intent="lookup", metric="fallout_rate", entity="PJM", region="East",
                          segment="Telemarketing"), _result())
    assert len(sel.records) == 1 and sel.calm is True
    rec = sel.records[0]
    assert rec["actual"] == 0.11 and rec["actual_method"] == "real"
    assert rec["metric"] == "fallout_rate" and rec["retrieved_context"] == ""


def test_lookup_flagged_channel_is_not_calm():
    sel = qs.select(_spec(intent="lookup", metric="cost_per_acquisition", entity="ERCOT",
                          region="North", segment="Door_to_Door"), _result())
    assert len(sel.records) == 1 and sel.calm is False


def test_lookup_no_data_is_empty():
    sel = qs.select(_spec(intent="lookup", metric="cost_per_acquisition", entity="PJM",
                          region="East", segment="Telemarketing"), _result())
    assert sel.records == []
