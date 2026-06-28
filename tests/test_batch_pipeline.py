"""Tests for the batch pipeline orchestrator (Build Sequence §19 step 6).

Pins the one piece of logic that belongs to the orchestrator and nowhere else — the generate →
validate → regenerate retry loop and its honest fallback — plus the severity split (HIGH/MEDIUM are
narrated, INFO/LOW skip the LLM). Hermetic: the LLM client is a stub (no network, no SDK), so the
guarantee is demonstrable without a model in the loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.variance_engine import PipelineError  # noqa: E402
from app.orchestration import batch_pipeline as bp  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402

KNOBS = {"numbers": "withhold", "style": "paragraph", "audience": "exec"}

# A clean placeholder-prose reply that references only a legal placeholder → passes the validator.
CLEAN = "CPA is running {variance_pct} above plan; review field commissions this week."
# A reply with a bare digit typed into prose → a stray numeral → always flagged.
STRAY = "CPA is running 5 percent above plan; review commissions."


class RecordingClient:
    """A call_llm-shaped stub that returns scripted replies in order (last repeats) and counts calls."""

    def __init__(self, *replies: str):
        self.replies = list(replies) or [CLEAN]
        self.calls = 0

    def __call__(self, system: str, user: str, *, model=None) -> str:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


class ExplodingClient:
    """A stub that fails the test if the LLM is ever called (proves INFO/LOW skip narration)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, system: str, user: str, *, model=None) -> str:
        self.calls += 1
        raise AssertionError("LLM client must not be called for low-priority findings")


def _finding(level: str) -> dict:
    return {**cpa_finding(), "risk_level": level}


# ---------------------------------------------------------------------------
# The retry loop
# ---------------------------------------------------------------------------
def test_happy_path_validates_and_fills_in_one_call():
    client = RecordingClient(CLEAN)
    out = bp.narrate_findings([_finding("HIGH")], client=client, knobs=KNOBS)
    assert client.calls == 1
    assert out[0]["validated"] is True
    assert out[0]["validation_flags"] == []
    # The placeholder was Python-filled with the formatted value (variance_pct 28.59 → "28.6%").
    assert "28.6%" in out[0]["narrative_filled"]
    assert "{variance_pct}" not in out[0]["narrative_filled"]


def test_retry_then_succeed():
    client = RecordingClient(STRAY, CLEAN)            # first flagged, second clean
    out = bp.narrate_findings([_finding("HIGH")], client=client, knobs=KNOBS)
    assert client.calls == 2
    assert out[0]["validated"] is True
    assert out[0]["validation_flags"] == []


def test_exhaustion_falls_back_after_three_attempts():
    client = RecordingClient(STRAY)                   # always flagged
    out = bp.narrate_findings([_finding("HIGH")], client=client, knobs=KNOBS)
    assert client.calls == bp.DEFAULT_MAX_ATTEMPTS    # exactly 3 attempts, then give up
    f = out[0]
    assert f["validated"] is False
    assert bp.FALLBACK_FLAG in f["validation_flags"]
    assert "stray_numeral:5" in f["validation_flags"]  # the last real flags are retained
    # Display is the honest, marked facts line; the last raw prose is kept for audit.
    assert f["narrative_filled"] == bp._facts_line(f)
    assert f["narrative_filled"].startswith("⚠ UNVERIFIED NARRATIVE")
    assert f["narrative"] == STRAY


@pytest.mark.parametrize("level", ["INFO", "LOW"])
def test_low_priority_findings_skip_the_llm(level):
    client = ExplodingClient()
    finding = _finding(level)
    out = bp.narrate_findings([finding], client=client, knobs=KNOBS)
    assert client.calls == 0                          # the LLM was never invoked
    assert out[0]["validated"] is True
    assert out[0]["narrative_filled"] == bp._data_block(finding)
    # A data block is a legitimate line — not the "unverified" fallback framing.
    assert "UNVERIFIED" not in out[0]["narrative_filled"]


# ---------------------------------------------------------------------------
# Number safety of the deterministic lines (Python owns every digit, §4)
# ---------------------------------------------------------------------------
def test_facts_line_renders_numbers_through_python():
    f = _finding("HIGH")
    line = bp._facts_line(f)
    # Headline numbers appear formatted by Python (currency / percent), never raw model digits.
    assert "$160.74" in line and "28.6%" in line and "HIGH" in line


# ---------------------------------------------------------------------------
# Fail-loud stage wrapper (§17)
# ---------------------------------------------------------------------------
def test_stage_wraps_failures_with_stage_name():
    def boom():
        raise ValueError("kaboom")

    with pytest.raises(PipelineError) as exc:
        bp._stage("narrate", boom)
    assert "narrate" in str(exc.value) and "kaboom" in str(exc.value)


def test_stage_passes_pipeline_errors_through_untouched():
    def boom():
        raise PipelineError("variance_engine: stage 'merge' failed: bad join")

    with pytest.raises(PipelineError) as exc:
        bp._stage("analytics", boom)
    assert "variance_engine" in str(exc.value)         # original context preserved, not re-wrapped
