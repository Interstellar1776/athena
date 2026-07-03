"""Tests for the conversational query pipeline (Build Sequence §19 step 9).

End-to-end with an injected analytics result + a STUB client that answers BOTH LLM calls (the router and
the narration) off the same seam — no network. Pins the §17 off-ramps (unclear / off-vocabulary / no
match) and, crucially, that the number-safety spine is in the loop: a hallucinated digit in the
narration never survives as a trusted answer (it falls back to the marked, number-safe facts line).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.orchestration import query_pipeline as qp  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402
from tests.test_query_selector import _assessments  # noqa: E402

SNAPSHOT = dt.date(2024, 5, 22)


class DualStub:
    """Answers both LLM calls: the router (returns a spec JSON) and the narrator (returns prose).

    It branches on the system prompt — the router's says 'JSON object', the narrator's says 'alert'."""

    def __init__(self, *, spec_json: str, narrative: str = "CPA is {variance_pct} above plan."):
        self.spec_json = spec_json
        self.narrative = narrative

    def __call__(self, system: str, user: str, *, model=None) -> str:
        return self.spec_json if "JSON object" in system else self.narrative


def _result() -> dict:
    # Findings carry the retrieved_context slot; empty notes/GL frames ground to "" (hermetic).
    return {"findings": [{**cpa_finding()}], "assessments": _assessments(), "snapshot_date": SNAPSHOT,
            "operational_notes": pd.DataFrame(), "gl_mapping": pd.DataFrame()}


_EXPLAIN_SPEC = ('{"intent":"explain","metric":"cost_per_acquisition","entity":"ERCOT",'
                 '"region":"North","segment":"Door_to_Door"}')


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_explain_produces_a_grounded_number_safe_answer():
    stub = DualStub(spec_json=_EXPLAIN_SPEC)
    ans = qp.answer_query("Why is CPA up in ERCOT North door-to-door?", client=stub, result=_result())
    assert ans["status"] == "ok"
    assert "28.6%" in ans["answer"]                          # Python filled {variance_pct}, not the LLM
    assert ans["matched"][0]["metric"] == "cost_per_acquisition"


def test_lookup_calm_channel_states_the_value_without_the_llm():
    spec = ('{"intent":"lookup","metric":"fallout_rate","entity":"PJM","region":"East",'
            '"segment":"Telemarketing"}')
    # Narration would be gibberish, but a calm (LOW) record takes the deterministic data-block path.
    stub = DualStub(spec_json=spec, narrative="raw 12345 nonsense")
    ans = qp.answer_query("What's fallout for PJM East telemarketing?", client=stub, result=_result())
    assert ans["status"] == "ok"
    assert "12345" not in ans["answer"]                      # LLM prose is not used for a calm lookup
    assert "fallout_rate" in ans["answer"]


# ---------------------------------------------------------------------------
# §17 off-ramps — plain language, never a blank or a traceback
# ---------------------------------------------------------------------------
def test_unclear_question_asks_for_a_rephrase():
    ans = qp.answer_query("How's the weather?", client=DualStub(spec_json='{"intent":null}'),
                          result=_result())
    assert ans["status"] == "unclear" and "couldn't tell" in ans["answer"]


def test_offvocab_filter_names_what_is_tracked():
    spec = '{"intent":"explain","metric":"cost_per_acquisition","entity":"Midwest"}'
    ans = qp.answer_query("Why is CPA up in the Midwest?", client=DualStub(spec_json=spec),
                          result=_result())
    assert ans["status"] == "unresolved"
    assert "Midwest" in ans["answer"] and "ERCOT" in ans["answer"]


def test_no_match_explain_says_it_is_calm():
    spec = ('{"intent":"explain","metric":"fallout_rate","entity":"PJM","region":"East",'
            '"segment":"Telemarketing"}')
    ans = qp.answer_query("Why is fallout up in PJM East?", client=DualStub(spec_json=spec),
                          result=_result())
    assert ans["status"] == "no_match" and "within plan" in ans["answer"]


# ---------------------------------------------------------------------------
# The spine holds in chat — a hallucinated number cannot surface as trusted
# ---------------------------------------------------------------------------
def test_hallucinated_number_falls_back_to_the_marked_facts_line():
    stub = DualStub(spec_json=_EXPLAIN_SPEC, narrative="CPA jumped to 999 this week.")
    ans = qp.answer_query("Why is CPA up in ERCOT North?", client=stub, result=_result())
    assert "999" not in ans["answer"]                        # the invented digit never reaches the user
    assert "UNVERIFIED" in ans["answer"]                     # honest, marked fallback (§17)
    assert ans["matched"][0]["risk_level"] == "HIGH"
