#!/usr/bin/env python3
"""context_retriever.py — match grounding context to each finding (Build Sequence §19 step 7).

Fills ``finding["retrieved_context"]`` — empty since step 4 — so the narrative can hypothesize a cause
grounded in something real (a CPA spike in ERCOT North door-to-door picks up the field-sales
commission note), and so the validator's provenance check has a source to allow contextual numbers
against (context doc §4; ``narrative_validator``).

Two context sources, two mechanisms — deliberately different:

* **Operational notes → metadata filtering (default) or semantic search.** Each note is tagged with
  ``entity/region/segment/date`` (``ALL`` = a per-level wildcard). ``strategy="filter"`` (default) is a
  structured match, ranked by *specificity* then *recency*. ``strategy="semantic"`` embeds the notes and
  a finding-derived query and ranks by cosine similarity (``app.retrieval.embeddings``; Ollama
  ``nomic-embed`` + numpy). Both were built for step 8's honest RAG-vs-filtering bake-off against
  realistic extracted notes (``docs/open_questions.md``, ``decisions_log`` BS8); filtering stays the
  default. The ``embedder`` is injectable so the semantic path is hermetically testable without Ollama.
* **GL descriptions → deterministic lookup.** A finding's channel ``(entity, region, segment)`` maps
  straight to its ``cost_center_description`` / ``gl_account_description`` via ``gl_mapping`` — a join,
  not a fuzzy match — so the model sees *what this channel's spend actually is*.

Pure functions, no LLM, no I/O — the orchestrator passes the already-loaded frames in.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# The dimensions a note is scoped by; "ALL" matches any value at that level (data-dictionary §convention).
SCOPE_DIMS = ("entity", "region", "segment")
WILDCARD = "ALL"

# An embedder is any ``list[str] -> (n, dim) float matrix`` callable; injected so the semantic path is
# testable without Ollama. Defaults to the real Ollama embedder (app.retrieval.embeddings).
Embedder = Callable[[list[str]], "np.ndarray"]


def _note_dict(note: pd.Series) -> dict:
    """Render a note row into the plain dict the generator/validator consume."""
    return {"date": note["date"].date().isoformat(),
            "scope": f"{note['entity']}/{note['region']}/{note['segment']}",
            "note_text": note["note_text"], "author": note.get("author", "")}


def _eligible(notes_df: pd.DataFrame, snapshot_date: dt.date) -> pd.DataFrame:
    """Date guard — never surface a note dated after the snapshot (no future context leak)."""
    return notes_df[notes_df["date"].dt.date <= snapshot_date]


# ===========================================================================
# 1a. Operational notes — metadata filtering (specificity → recency)
# ===========================================================================
def _matches(note: pd.Series, finding: dict) -> bool:
    """A note matches a finding iff every scope dim equals the finding's value or is the wildcard."""
    return all(note[d] == finding.get(d) or note[d] == WILDCARD for d in SCOPE_DIMS)


def _specificity(note: pd.Series, finding: dict) -> int:
    """How tightly the note is scoped to this finding: count of exact (non-wildcard) dim matches.
    An exactly-scoped note (3) outranks an org-wide ``ALL/ALL/ALL`` note (0)."""
    return sum(1 for d in SCOPE_DIMS if note[d] == finding.get(d) and note[d] != WILDCARD)


def _retrieve_filter(finding: dict, eligible: pd.DataFrame, top_k: int) -> list[dict]:
    """Metadata filtering: keep matching notes, rank by specificity desc then recency desc."""
    scored = [(_specificity(note, finding), note["date"], note)
              for _, note in eligible.iterrows() if _matches(note, finding)]
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [_note_dict(note) for _, _, note in scored[:top_k]]


# ===========================================================================
# 1b. Operational notes — semantic search (embedding + cosine; step-8 arm)
# ===========================================================================
def _finding_query(finding: dict) -> str:
    """The natural-language query a finding poses to the note corpus — its channel + what moved."""
    return " ".join(str(finding.get(k, "")) for k in
                    (*SCOPE_DIMS, "metric", "alert_type")).strip()


def _retrieve_semantic(finding: dict, eligible: pd.DataFrame, top_k: int, embedder: Embedder) -> list[dict]:
    """Semantic ranking: embed the finding query + each eligible note, rank by cosine similarity.

    One embedder call (query first, notes after) keeps stubbing simple and avoids a second round-trip."""
    from app.retrieval.embeddings import cosine_rank

    texts = [str(t) for t in eligible["note_text"]]
    vectors = embedder([_finding_query(finding), *texts])
    query_vec, note_matrix = vectors[0], vectors[1:]
    ranked = cosine_rank(query_vec, note_matrix)[:top_k]
    return [_note_dict(eligible.iloc[i]) for i, _ in ranked]


def retrieve_notes(finding: dict, notes_df: pd.DataFrame, *, snapshot_date: dt.date,
                   top_k: int = 3, strategy: str = "filter", embedder: Embedder | None = None) -> list[dict]:
    """Return up to ``top_k`` operational notes relevant to ``finding``, most-relevant first.

    ``strategy="filter"`` (default) — metadata filtering (specificity → recency). ``strategy="semantic"``
    — embedding + cosine similarity; ``embedder`` (a ``list[str] -> matrix`` callable) is injectable for
    hermetic tests and defaults to the Ollama embedder (``app.retrieval.embeddings.embed_texts``).
    """
    if strategy not in ("filter", "semantic"):
        raise ValueError(f"unknown retrieval strategy {strategy!r} — expected 'filter' or 'semantic'.")
    if notes_df is None or notes_df.empty:
        return []

    eligible = _eligible(notes_df, snapshot_date)
    if eligible.empty:
        return []

    if strategy == "filter":
        return _retrieve_filter(finding, eligible, top_k)

    if embedder is None:                                     # default to the real Ollama embedder
        from app.retrieval.embeddings import embed_texts
        embedder = embed_texts
    return _retrieve_semantic(finding, eligible, top_k, embedder)


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
                  snapshot_date: dt.date, top_k: int = 3, strategy: str = "filter",
                  embedder: Embedder | None = None) -> str:
    """Assemble ``retrieved_context``: GL descriptions (what the channel is) then matched notes (why).

    Plain text, so it drops into the generator's existing ``OPERATIONAL CONTEXT:`` slot and the
    validator can do verbatim-substring provenance against it. Empty string when nothing matches (the
    generator renders that as "(none provided)")."""
    gl = gl_descriptions(finding, gl_mapping_df)
    notes = retrieve_notes(finding, notes_df, snapshot_date=snapshot_date, top_k=top_k,
                           strategy=strategy, embedder=embedder)

    parts: list[str] = []
    if gl:
        parts.append("CHANNEL SPEND (from the general ledger):\n"
                     + "\n".join(f"- {line}" for line in gl))
    if notes:
        parts.append("OPERATIONAL NOTES:\n"
                     + "\n".join(f"- {n['date']} ({n['scope']}): {n['note_text']}" for n in notes))
    return "\n\n".join(parts)


def attach_context(findings: list[dict], notes_df: pd.DataFrame, gl_mapping_df: pd.DataFrame, *,
                   snapshot_date: dt.date, top_k: int = 3, strategy: str = "filter",
                   embedder: Embedder | None = None) -> list[dict]:
    """Return new findings with ``retrieved_context`` populated. Applied to every finding — filtering is
    free, and uniform context aids drill-down even on findings that won't be narrated."""
    out = [{**f, "retrieved_context": build_context(
                f, notes_df, gl_mapping_df, snapshot_date=snapshot_date, top_k=top_k,
                strategy=strategy, embedder=embedder)}
           for f in findings]
    n_grounded = sum(1 for f in out if f["retrieved_context"])
    logger.info("context_retriever: attached context to %d/%d findings (strategy=%s, top_k=%d)",
                n_grounded, len(out), strategy, top_k)
    return out
