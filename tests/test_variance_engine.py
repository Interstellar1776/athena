"""Tests for the variance engine (Build Sequence §19 step 3 — the orchestrator).

variance_engine threads the pure cores once and returns a rich result. The decisive test is that its
findings are **identical** to running the chain via findings_builder's own wrapper — proving the single
pass wires the cores correctly. Also: the rich result carries every documented key, the summary is
consistent, a stage failure halts loudly naming the stage, an empty feed is returned cleanly, and runs
are deterministic.

Runs against the real configured snapshot (2024-05-22).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics import variance_engine as ve                  # noqa: E402
from app.analytics.findings_builder import compute_findings      # noqa: E402
from app.analytics.risk_classifier import compute_assessments    # noqa: E402

RESULT_KEYS = {"snapshot_date", "current_period", "findings", "assessments", "metrics",
               "projection", "gl_states", "merged", "summary"}


def _finding_key(findings):
    return [(f["finding_id"], f["alert_type"], f["entity"], f["region"], f["segment"],
             f["period"], f["risk_level"], round(f["variance_pct"], 4)
             if f["variance_pct"] is not None and not pd.isna(f["variance_pct"]) else None)
            for f in findings]


import pandas as pd  # noqa: E402  (after sys.path)


@pytest.fixture(scope="module")
def result():
    return ve.run_pipeline()


# ---------------------------------------------------------------------------
# Rich result shape + summary
# ---------------------------------------------------------------------------
def test_result_has_all_documented_keys(result):
    assert RESULT_KEYS <= set(result)


def test_intermediates_present_from_one_pass(result):
    assert {"cpa", "economics", "fallout"} <= set(result["metrics"])
    assert {"volume_projection", "fallout_projection"} <= set(result["projection"])
    assert hasattr(result["gl_states"], "shape")
    assert "gl_acquisition" in result["merged"]


def test_summary_is_consistent(result):
    s = result["summary"]
    assert s["n_findings"] == len(result["findings"])
    assert s["n_assessments"] == len(result["assessments"])
    assert sum(s["by_risk_level"].values()) == len(result["findings"])


# ---------------------------------------------------------------------------
# The decisive test — one pass == the module wrappers
# ---------------------------------------------------------------------------
def test_findings_identical_to_findings_builder(result):
    expected = compute_findings()["findings"]
    assert _finding_key(result["findings"]) == _finding_key(expected)


def test_assessments_match_risk_classifier(result):
    expected = compute_assessments()["assessments"]
    assert len(result["assessments"]) == len(expected)
    got = result["assessments"].groupby(["alert_type", "risk_level"]).size().to_dict()
    assert got == expected.groupby(["alert_type", "risk_level"]).size().to_dict()


# ---------------------------------------------------------------------------
# Fail-loud per stage (§17) + empty feed + determinism
# ---------------------------------------------------------------------------
def test_stage_failure_halts_loudly_naming_the_stage(monkeypatch):
    def boom(*a, **k):
        raise ValueError("synthetic")
    monkeypatch.setattr(ve, "classify", boom)
    with pytest.raises(ve.PipelineError, match=r"stage 'risk_classifier'"):
        ve.run_pipeline()


def test_empty_feed_is_returned_cleanly(monkeypatch):
    monkeypatch.setattr(ve, "build_findings", lambda *a, **k: [])
    r = ve.run_pipeline()
    assert r["findings"] == [] and r["summary"]["n_findings"] == 0
    assert r["summary"]["by_risk_level"] == {}                   # no crash on an empty feed


def test_deterministic_across_runs():
    assert _finding_key(ve.run_pipeline()["findings"]) == _finding_key(ve.run_pipeline()["findings"])
