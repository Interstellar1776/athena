"""Tests for the note extractor (Build Sequence §19 step 8).

Pins the transcript→notes contract with a STUB client (no network, no SDK): the prompt carries the
transcript, the valid-dimension dictionary, and the numbers-stay-verbatim rule; extraction yields rows
in the `operational_notes` schema; and the two guards hold — an invalid scope tag is **discarded**, an
**ungrounded number** (absent from the transcript) is **discarded**, while a malformed model reply fails
loud. Closes the loop by feeding the result through `context_retriever` (filter) and confirming the
hero cause is reconstructed. Hermetic — synthetic transcript + stubbed LLM.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.retrieval import context_retriever as cr  # noqa: E402
from app.retrieval import note_extractor as nx  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402

SNAPSHOT = dt.date(2024, 5, 22)

# A short transcript carrying the hero cause and a verbatim number.
TRANSCRIPT = (
    "Weekly Ops Sync — May 7, 2024.\n"
    "Rivera: On May 6 we pushed the ERCOT North door-to-door team hard — raised commissions and stood "
    "up an incentive bonus. Expect elevated cost per acquisition near term.\n"
    "Osei: We also found a late April invoice, a ~$9.8k overage, landing in May."
)


def _allowed() -> dict[str, set[str]]:
    """The dimension dictionary the extractor tags against (as `derive_allowed_scopes` would build)."""
    return {"entity": {"ALL", "ERCOT", "PJM"}, "region": {"ALL", "North", "South", "West", "East"},
            "segment": {"ALL", "Door_to_Door", "Telemarketing", "Direct_Mail"}}


class StubClient:
    """A call_llm-shaped stub that records prompts and returns a fixed JSON-array reply."""

    def __init__(self, rows: list[dict], *, wrap: str = "{}"):
        # `wrap` lets a test simulate model chatter/fences around the JSON array.
        self.reply = wrap.format(json.dumps(rows))
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, *, model=None) -> str:
        self.calls.append((system, user))
        return self.reply


def _row(**over) -> dict:
    base = {"date": "2024-05-07", "entity": "ERCOT", "region": "North", "segment": "Door_to_Door",
            "note_text": "Raised commissions and stood up an incentive bonus on May 6.",
            "author": "J. Rivera"}
    return {**base, **over}


# ---------------------------------------------------------------------------
# derive_allowed_scopes — the dictionary fed from a dataframe
# ---------------------------------------------------------------------------
def test_derive_allowed_scopes_reads_the_frame_and_adds_wildcard():
    df = pd.DataFrame({"entity": ["ERCOT", "PJM"], "region": ["North", "East"],
                       "segment": ["Door_to_Door", "Direct_Mail"], "other": [1, 2]})
    scopes = nx.derive_allowed_scopes(df)
    assert scopes["entity"] == {"ALL", "ERCOT", "PJM"}
    assert "ALL" in scopes["region"] and "North" in scopes["region"]


def test_derive_allowed_scopes_fails_loud_without_scope_dims():
    with pytest.raises(nx.ExtractionError):
        nx.derive_allowed_scopes(pd.DataFrame({"foo": [1]}))


# ---------------------------------------------------------------------------
# build_extraction_prompt
# ---------------------------------------------------------------------------
def test_prompt_carries_transcript_dictionary_and_number_rule():
    system, user = nx.build_extraction_prompt(TRANSCRIPT, allowed_scopes=_allowed())
    assert "never compute, round" in system.lower() or "verbatim" in system.lower()
    assert "Door_to_Door" in user                        # the allowed-values dictionary is in the prompt
    assert "May 6" in user                               # the transcript is in the prompt
    assert "JSON array" in system


# ---------------------------------------------------------------------------
# extract_notes — schema, tolerant parsing
# ---------------------------------------------------------------------------
def test_extract_returns_operational_notes_schema():
    stub = StubClient([_row()])
    rows = nx.extract_notes(TRANSCRIPT, client=stub, allowed_scopes=_allowed())
    assert len(rows) == 1
    assert set(rows[0]) == set(nx.NOTE_COLUMNS)
    assert rows[0]["entity"] == "ERCOT" and rows[0]["segment"] == "Door_to_Door"


def test_parse_tolerates_prose_and_fences_around_the_array():
    stub = StubClient([_row()], wrap="Sure! Here you go:\n```json\n{}\n```\nDone.")
    rows = nx.extract_notes(TRANSCRIPT, client=stub, allowed_scopes=_allowed())
    assert len(rows) == 1


def test_df_has_exact_columns_in_order():
    stub = StubClient([_row()])
    df = nx.extract_notes_df([TRANSCRIPT], client=stub, allowed_scopes=_allowed())
    assert list(df.columns) == list(nx.NOTE_COLUMNS)


# ---------------------------------------------------------------------------
# Guards — discard invalid scope / ungrounded number; fail loud on malformed
# ---------------------------------------------------------------------------
def test_invalid_scope_is_discarded_valid_is_kept():
    stub = StubClient([_row(entity="MADE_UP"), _row()])   # first has an unknown entity → dropped
    rows = nx.extract_notes(TRANSCRIPT, client=stub, allowed_scopes=_allowed())
    assert len(rows) == 1 and rows[0]["entity"] == "ERCOT"


def test_grounded_number_passes_ungrounded_is_discarded():
    grounded = _row(note_text="Late April invoice, a ~$9.8k overage, landing in May.")   # $9.8k in transcript
    fabricated = _row(note_text="CPA jumped to $214 this week.")                          # 214 not in transcript
    rows = nx.extract_notes(TRANSCRIPT, client=StubClient([grounded, fabricated]),
                            allowed_scopes=_allowed())
    assert len(rows) == 1 and "9.8k" in rows[0]["note_text"]


def test_bad_date_is_discarded():
    rows = nx.extract_notes(TRANSCRIPT, client=StubClient([_row(date="last Tuesday")]),
                            allowed_scopes=_allowed())
    assert rows == []


def test_malformed_reply_fails_loud():
    class Bad:
        def __call__(self, system, user, *, model=None):
            return "I couldn't find anything to extract."
    with pytest.raises(nx.ExtractionError):
        nx.extract_notes(TRANSCRIPT, client=Bad(), allowed_scopes=_allowed())


# ---------------------------------------------------------------------------
# End-to-end — extracted notes feed context_retriever and reconstruct the cause
# ---------------------------------------------------------------------------
def test_extracted_notes_reconstruct_the_hero_cause_via_retrieval():
    stub = StubClient([_row(note_text="Raised commissions and stood up an incentive bonus for the "
                                      "door-to-door field team on May 6.")])
    notes_df = nx.extract_notes_df([TRANSCRIPT], client=stub, allowed_scopes=_allowed())
    notes_df["date"] = pd.to_datetime(notes_df["date"])            # retriever expects datetime64
    retrieved = cr.retrieve_notes(cpa_finding(), notes_df, snapshot_date=SNAPSHOT)
    assert any("commissions" in n["note_text"] for n in retrieved)
