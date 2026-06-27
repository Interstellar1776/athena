#!/usr/bin/env python3
"""context_retriever.py — match grounding context to each finding (Build Sequence §19 step 7).

Fills ``finding["retrieved_context"]`` — empty since step 4 — so the narrative can hypothesize a cause
grounded in something real (a CPA spike in ERCOT North door-to-door picks up the field-sales
commission note), and so the validator's provenance check has a source to allow contextual numbers
against (context doc §4; ``narrative_validator``).

Two context sources, two mechanisms — deliberately different:

* **Operational notes → metadata filtering.** Each note is tagged with ``entity/region/segment/date``
  (``ALL`` = a per-level wildcard). Matching is a structured filter, ranked by *specificity* then
  *recency*. Not RAG: the corpus is tiny and already cleanly tagged, so semantic search would add an
  embedding dependency for no gain. A ``strategy`` seam leaves the door open; the honest
  semantic-vs-filtering bake-off belongs at step 8, against realistic messy transcripts
  (``docs/open_questions.md``).
* **GL descriptions → deterministic lookup.** A finding's channel ``(entity, region, segment)`` maps
  straight to its ``cost_center_description`` / ``gl_account_description`` via ``gl_mapping`` — a join,
  not a fuzzy match — so the model sees *what this channel's spend actually is*.

Pure functions, no LLM, no I/O — the orchestrator passes the already-loaded frames in.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

logger = logging.getLogger(__name__)

# The dimensions a note is scoped by; "ALL" matches any value at that level (data-dictionary §convention).
SCOPE_DIMS = ("entity", "region", "segment")
WILDCARD = "ALL"


# ===========================================================================
# 1. Operational notes — metadata filtering (specificity → recency)
# ===========================================================================
def _matches(note: pd.Series, finding: dict) -> bool:
    """A note matches a finding iff every scope dim equals the finding's value or is the wildcard."""
    return all(note[d] == finding.get(d) or note[d] == WILDCARD for d in SCOPE_DIMS)


def _specificity(note: pd.Series, finding: dict) -> int:
    """How tightly the note is scoped to this finding: count of exact (non-wildcard) dim matches.
    An exactly-scoped note (3) outranks an org-wide ``ALL/ALL/ALL`` note (0)."""
    return sum(1 for d in SCOPE_DIMS if note[d] == finding.get(d) and note[d] != WILDCARD)


def retrieve_notes(finding: dict, notes_df: pd.DataFrame, *, snapshot_date: dt.date,
                   top_k: int = 3, strategy: str = "filter") -> list[dict]:
    """Return up to ``top_k`` operational notes relevant to ``finding``, most-relevant first.

    ``strategy="filter"`` (default) — metadata filtering. ``strategy="semantic"`` is the documented
    extension point (embedding + cosine), intentionally not built here; see the module docstring.
    """
    if strategy == "semantic":
        raise NotImplementedError(
            "semantic retrieval is the deferred step-8 arm (RAG-vs-filtering bake-off against real "
            "transcripts) — see docs/open_questions.md; use strategy='filter'.")
    if strategy != "filter":
        raise ValueError(f"unknown retrieval strategy {strategy!r} — expected 'filter' or 'semantic'.")
    if notes_df is None or notes_df.empty:
        return []

    # Date guard — never surface a note dated after the snapshot (no future context leak).
    eligible = notes_df[notes_df["date"].dt.date <= snapshot_date]

    scored = []
    for _, note in eligible.iterrows():
        if _matches(note, finding):
            scored.append((_specificity(note, finding), note["date"], note))

    # Rank: specificity desc, then recency desc. (Sort by a key tuple; both descending.)
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    return [{"date": note["date"].date().isoformat(),
             "scope": f"{note['entity']}/{note['region']}/{note['segment']}",
             "note_text": note["note_text"], "author": note.get("author", "")}
            for _, _, note in scored[:top_k]]


# ===========================================================================
# 2. GL descriptions — deterministic lookup (what this channel's spend is)
# ===========================================================================
def gl_descriptions(finding: dict, gl_mapping_df: pd.DataFrame) -> list[str]:
    """The distinct ``"<cost_center_description> — <gl_account_description>"`` lines for the finding's
    channel — a direct ``gl_mapping`` lookup on ``(entity, region, segment)``. Empty when the finding's
    grain has no acquisition-spend mapping (e.g. a non-acquisition segment)."""
    if gl_mapping_df is None or gl_mapping_df.empty:
        return []
    mask = pd.Series(True, index=gl_mapping_df.index)
    for d in SCOPE_DIMS:
        mask &= gl_mapping_df[d] == finding.get(d)
    rows = gl_mapping_df[mask]
    seen, out = set(), []
    for _, r in rows.iterrows():
        line = f"{r['cost_center_description']} — {r['gl_account_description']}"
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


# ===========================================================================
# 3. Assemble the context string the generator/validator consume
# ===========================================================================
def build_context(finding: dict, notes_df: pd.DataFrame, gl_mapping_df: pd.DataFrame, *,
                  snapshot_date: dt.date, top_k: int = 3, strategy: str = "filter") -> str:
    """Assemble ``retrieved_context``: GL descriptions (what the channel is) then matched notes (why).

    Plain text, so it drops into the generator's existing ``OPERATIONAL CONTEXT:`` slot and the
    validator can do verbatim-substring provenance against it. Empty string when nothing matches (the
    generator renders that as "(none provided)")."""
    gl = gl_descriptions(finding, gl_mapping_df)
    notes = retrieve_notes(finding, notes_df, snapshot_date=snapshot_date, top_k=top_k, strategy=strategy)

    parts: list[str] = []
    if gl:
        parts.append("CHANNEL SPEND (from the general ledger):\n"
                     + "\n".join(f"- {line}" for line in gl))
    if notes:
        parts.append("OPERATIONAL NOTES:\n"
                     + "\n".join(f"- {n['date']} ({n['scope']}): {n['note_text']}" for n in notes))
    return "\n\n".join(parts)


def attach_context(findings: list[dict], notes_df: pd.DataFrame, gl_mapping_df: pd.DataFrame, *,
                   snapshot_date: dt.date, top_k: int = 3, strategy: str = "filter") -> list[dict]:
    """Return new findings with ``retrieved_context`` populated. Applied to every finding — filtering is
    free, and uniform context aids drill-down even on findings that won't be narrated."""
    out = [{**f, "retrieved_context": build_context(
                f, notes_df, gl_mapping_df, snapshot_date=snapshot_date, top_k=top_k, strategy=strategy)}
           for f in findings]
    n_grounded = sum(1 for f in out if f["retrieved_context"])
    logger.info("context_retriever: attached context to %d/%d findings (strategy=%s, top_k=%d)",
                n_grounded, len(out), strategy, top_k)
    return out
