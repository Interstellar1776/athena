"""Tests for the context retriever (Build Sequence §19 step 7).

Pins the grounding contract: operational notes are matched by metadata (entity/region/segment/date,
"ALL" = wildcard) ranked by specificity then recency; GL descriptions are a deterministic lookup; and
the assembled context is what the generator/validator consume. Hermetic — synthetic notes/findings,
no pipeline, no LLM.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.retrieval import context_retriever as cr  # noqa: E402
from tests.test_placeholders import cpa_finding  # noqa: E402

SNAPSHOT = dt.date(2024, 5, 22)


def _notes() -> pd.DataFrame:
    rows = [
        # org-wide wildcard note (matches anything, lowest specificity)
        ("2024-04-15", "ALL", "ALL", "ALL", "Annual compliance training scheduled.", "People Ops"),
        # the hero cause — exactly scoped to the CPA finding
        ("2024-05-07", "ERCOT", "North", "Door_to_Door",
         "Pushed the door-to-door field team hard — raised commissions and bonuses.", "J. Rivera"),
        # benign note in a different channel — must NOT match
        ("2024-05-03", "PJM", "East", "Direct_Mail", "Print vendor renewed; terms unchanged.", "Partners"),
        # future note — must be excluded by the date guard
        ("2024-06-01", "ERCOT", "North", "Door_to_Door", "Post-snapshot retro planning.", "Ops"),
    ]
    df = pd.DataFrame(rows, columns=["date", "entity", "region", "segment", "note_text", "author"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _gl_mapping() -> pd.DataFrame:
    rows = [
        ("5020", "Door-to-Door Field Sales", "6030", "Commissions", "FieldForce", "Door_to_Door", "ERCOT", "North"),
        ("5020", "Door-to-Door Field Sales", "6040", "Bonuses & Incentives", "FieldForce", "Door_to_Door", "ERCOT", "North"),
        ("5010", "Web Direct", "6010", "Media", "SearchAds", "Web_Direct", "ERCOT", "North"),
    ]
    return pd.DataFrame(rows, columns=["cost_center", "cost_center_description", "gl_account",
                                       "gl_account_description", "vendor", "segment", "entity", "region"])


# ---------------------------------------------------------------------------
# retrieve_notes — matching, ranking, guards
# ---------------------------------------------------------------------------
def test_matches_the_scoped_note_not_the_benign_one():
    notes = cr.retrieve_notes(cpa_finding(), _notes(), snapshot_date=SNAPSHOT)
    texts = " ".join(n["note_text"] for n in notes)
    assert "door-to-door field team" in texts          # the hero cause is retrieved
    assert "Print vendor" not in texts                 # the PJM/Direct_Mail note is not


def test_exact_scope_outranks_all_wildcard():
    notes = cr.retrieve_notes(cpa_finding(), _notes(), snapshot_date=SNAPSHOT)
    # The exactly-scoped May-7 note ranks above the ALL/ALL/ALL compliance note.
    assert notes[0]["scope"] == "ERCOT/North/Door_to_Door"
    assert any(n["scope"] == "ALL/ALL/ALL" for n in notes)   # wildcard still included, just lower


def test_date_guard_excludes_future_notes():
    notes = cr.retrieve_notes(cpa_finding(), _notes(), snapshot_date=SNAPSHOT)
    assert all(n["date"] <= SNAPSHOT.isoformat() for n in notes)
    assert not any("Post-snapshot" in n["note_text"] for n in notes)


def test_top_k_caps_results():
    notes = cr.retrieve_notes(cpa_finding(), _notes(), snapshot_date=SNAPSHOT, top_k=1)
    assert len(notes) == 1
    assert notes[0]["scope"] == "ERCOT/North/Door_to_Door"   # the most relevant survives the cap


def test_semantic_strategy_is_the_deferred_seam():
    with pytest.raises(NotImplementedError):
        cr.retrieve_notes(cpa_finding(), _notes(), snapshot_date=SNAPSHOT, strategy="semantic")


# ---------------------------------------------------------------------------
# gl_descriptions — deterministic lookup
# ---------------------------------------------------------------------------
def test_gl_descriptions_for_the_channel():
    lines = cr.gl_descriptions(cpa_finding(), _gl_mapping())
    assert "Door-to-Door Field Sales — Commissions" in lines
    assert "Door-to-Door Field Sales — Bonuses & Incentives" in lines
    assert "Web Direct — Media" not in lines             # a different channel's spend is not included


# ---------------------------------------------------------------------------
# build_context + attach_context — what the generator/validator consume
# ---------------------------------------------------------------------------
def test_build_context_combines_gl_and_notes():
    ctx = cr.build_context(cpa_finding(), _notes(), _gl_mapping(), snapshot_date=SNAPSHOT)
    assert "CHANNEL SPEND" in ctx and "Door-to-Door Field Sales — Commissions" in ctx
    assert "OPERATIONAL NOTES" in ctx and "door-to-door field team" in ctx


def test_attach_context_populates_retrieved_context():
    out = cr.attach_context([cpa_finding()], _notes(), _gl_mapping(), snapshot_date=SNAPSHOT)
    assert out[0]["retrieved_context"]                   # non-empty
    assert "door-to-door field team" in out[0]["retrieved_context"]


def test_no_sources_yields_empty_context():
    # No notes and no GL mapping → empty context (not an error). The generator renders "(none provided)".
    empty_notes = _notes().iloc[0:0]
    empty_gl = _gl_mapping().iloc[0:0]
    out = cr.attach_context([cpa_finding()], empty_notes, empty_gl, snapshot_date=SNAPSHOT)
    assert out[0]["retrieved_context"] == ""
