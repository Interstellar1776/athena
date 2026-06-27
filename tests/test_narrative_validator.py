"""Tests for the narrative validator (Build Sequence §19 step 5).

These ARE the "we catch every leak" proof — hermetic (synthetic findings + crafted narratives, no LLM).
They pin: clean prose fills and passes; an orphan placeholder flags; a stray numeral flags; a
digit-bearing placeholder name ({cpa_t3m}) is NOT a false positive; and validate_findings sets the
downstream fields while keeping the raw narrative.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.validation.narrative_validator import validate_findings, validate_narrative  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402


# ---------------------------------------------------------------------------
# Clean prose — fills and passes
# ---------------------------------------------------------------------------
def test_clean_narrative_fills_and_passes():
    f = cpa_finding()
    res = validate_narrative(
        "CPA is {variance_pct} above plan, with {actual} vs {reference} in {period}.", f)
    assert res.ok
    assert not res.flags
    assert res.filled == "CPA is 28.6% above plan, with $160.74 vs $125.00 in May 2024."


# ---------------------------------------------------------------------------
# Orphan placeholder — used but not legal for this finding
# ---------------------------------------------------------------------------
def test_orphan_placeholder_flags_and_stays_visible():
    f = cpa_finding()  # a CPA finding has no projection
    res = validate_narrative("CPA at {actual}; projected {projected_linear} by close.", f)
    assert not res.ok
    assert res.orphans == ["projected_linear"]
    assert "orphan_placeholder:{projected_linear}" in res.flags
    assert "{projected_linear}" in res.filled         # left visible, not silently dropped
    assert "$160.74" in res.filled                     # the legal one still filled


# ---------------------------------------------------------------------------
# Stray numeral — a bare digit typed into prose
# ---------------------------------------------------------------------------
def test_stray_numeral_flags():
    f = cpa_finding()
    res = validate_narrative("CPA is 28% above plan (about $160 per acquisition).", f)
    assert not res.ok
    assert set(res.strays) == {"28", "160"}
    assert any(flag.startswith("stray_numeral:") for flag in res.flags)


# ---------------------------------------------------------------------------
# The gotcha: a digit-bearing placeholder NAME must not be flagged as a stray
# ---------------------------------------------------------------------------
def test_digit_in_placeholder_name_is_not_a_false_positive():
    f = cpa_finding()  # supporting_metrics has cpa_t3m / cpa_t12m
    res = validate_narrative("Compare against trailing CPA {cpa_t3m} and {cpa_t12m}.", f)
    assert res.ok, res.flags
    assert not res.strays
    assert "{cpa_t3m}" not in res.filled and "{cpa_t12m}" not in res.filled   # both filled
    assert "$160.74" in res.filled                     # cpa_t3m rendered (value 160.74)


# ---------------------------------------------------------------------------
# validate_findings — sets fields, keeps raw narrative
# ---------------------------------------------------------------------------
def test_validate_findings_sets_fields_and_keeps_raw():
    raw = "CPA is {variance_pct} above plan."
    findings = [{**cpa_finding(), "narrative": raw}]
    out = validate_findings(findings)
    f = out[0]
    assert f["narrative"] == raw                        # raw kept (auditable)
    assert f["narrative_filled"] == "CPA is 28.6% above plan."
    assert f["validated"] is True
    assert f["validation_flags"] == []
    # original input not mutated
    assert "narrative_filled" not in findings[0]


def test_validate_findings_flags_a_bad_narrative():
    findings = [{**cpa_finding(), "narrative": "CPA jumped 28% — see {made_up}."}]
    f = validate_findings(findings)[0]
    assert f["validated"] is False
    assert any(x.startswith("stray_numeral:") for x in f["validation_flags"])
    assert "orphan_placeholder:{made_up}" in f["validation_flags"]


# ---------------------------------------------------------------------------
# Provenance (step 7) — a number traceable to retrieved_context is allowed
# ---------------------------------------------------------------------------
def test_contextual_number_in_retrieved_context_passes():
    # A digit quoted from the note ("$9.8k" → "9.8") is grounded, so it is NOT a stray.
    f = {**cpa_finding(),
         "retrieved_context": "OPERATIONAL NOTES:\n- 2024-05-19 (ERCOT/North/Door_to_Door): "
                              "Finance flagged a late April invoice (~$9.8k overage)."}
    res = validate_narrative("CPA is {variance_pct} above plan, tied to the $9.8k late invoice.", f)
    assert res.ok
    assert not res.strays


def test_same_number_without_matching_context_still_flags():
    # The identical digit is a stray when it traces to neither a placeholder nor the context.
    f = {**cpa_finding(), "retrieved_context": "OPERATIONAL NOTES:\n- training scheduled."}
    res = validate_narrative("CPA is {variance_pct} above plan, tied to the $9.8k late invoice.", f)
    assert not res.ok
    assert "9.8" in res.strays


def test_empty_context_keeps_the_strict_rule():
    # No context → the provenance allowance degenerates to "any bare digit is a stray".
    f = {**cpa_finding(), "retrieved_context": ""}
    res = validate_narrative("CPA rose 9.8 points.", f)
    assert not res.ok
    assert "9.8" in res.strays
