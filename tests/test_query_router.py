"""Tests for the query router (Build Sequence §19 step 9).

Pins the NL→structured-query contract with a STUB client (no network): the prompt hands the model the
vocabulary + the JSON rule; `route` parses and validates every field against that vocabulary; an
off-vocabulary value is surfaced in `unresolved` (not silently dropped); a malformed reply fails loud;
and an unrecognizable question yields `intent=None` (a clarification, not an error). Hermetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.llm import query_router as qr  # noqa: E402

VOCAB = {"metrics": ["cost_per_acquisition", "fallout_rate"],
         "entity": ["ERCOT", "PJM"], "region": ["North", "South", "East"],
         "segment": ["Door_to_Door", "Telemarketing"]}


class StubClient:
    """A call_llm-shaped stub returning a fixed reply string."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, *, model=None) -> str:
        self.calls.append((system, user))
        return self.reply


# ---------------------------------------------------------------------------
# build_router_prompt
# ---------------------------------------------------------------------------
def test_prompt_carries_vocabulary_and_json_rule():
    system, user = qr.build_router_prompt("Why is CPA up in ERCOT North?", vocab=VOCAB)
    assert "JSON object" in system
    assert "cost_per_acquisition" in user and "ERCOT" in user and "Door_to_Door" in user


# ---------------------------------------------------------------------------
# route — parse + validate
# ---------------------------------------------------------------------------
def test_route_parses_and_validates_a_clean_spec():
    reply = ('{"intent":"explain","metric":"cost_per_acquisition","entity":"ERCOT",'
             '"region":"North","segment":"Door_to_Door","reference_type":null}')
    spec = qr.route("Why is CPA up in ERCOT North door-to-door?", client=StubClient(reply), vocab=VOCAB)
    assert spec.intent == "explain" and spec.metric == "cost_per_acquisition"
    assert (spec.entity, spec.region, spec.segment) == ("ERCOT", "North", "Door_to_Door")
    assert spec.unresolved == []


def test_route_tolerates_prose_and_fences_around_the_object():
    reply = 'Sure:\n```json\n{"intent":"lookup","metric":"fallout_rate"}\n```\n'
    spec = qr.route("How's fallout?", client=StubClient(reply), vocab=VOCAB)
    assert spec.intent == "lookup" and spec.metric == "fallout_rate"


def test_offvocab_value_is_surfaced_not_dropped():
    reply = '{"intent":"explain","metric":"cost_per_acquisition","entity":"Midwest"}'
    spec = qr.route("Why is CPA up in the Midwest?", client=StubClient(reply), vocab=VOCAB)
    assert spec.entity is None                               # not kept as a bogus filter
    assert ("entity", "Midwest") in spec.unresolved         # but surfaced for a plain-language reply


def test_unrecognizable_question_yields_none_intent():
    spec = qr.route("How's the weather?", client=StubClient('{"intent":null}'), vocab=VOCAB)
    assert spec.intent is None


def test_malformed_reply_fails_loud():
    with pytest.raises(qr.RouterError):
        qr.route("anything", client=StubClient("I can't help with that."), vocab=VOCAB)
