#!/usr/bin/env python3
"""bakeoff_retrieval.py — the deferred RAG-vs-filtering bake-off, run for real (Build Sequence §19 step 8).

Step 7 shipped metadata filtering and **deferred** the filtering-vs-semantic comparison because the
clean `operational_notes.csv` stand-in made it meaningless (filtering wins trivially). Step 8 supplies
the honest input: notes **extracted from messy transcripts** by `note_extractor`. This harness runs both
retrieval strategies over those extracted notes and reports which surfaces the right context for each
seeded finding — the evidence recorded in `decisions_log.md` (BS8) that sets the default `strategy`.

Not hermetic: the extraction pass calls the configured LLM (`call_llm`) and the semantic arm calls
Ollama embeddings (`LLM_EMBED_MODEL`, e.g. `nomic-embed-text`). Run it locally with Ollama up:

    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b LLM_EMBED_MODEL=nomic-embed-text \\
        python -m scripts.eval.bakeoff_retrieval

The hermetic proof of each mechanism lives in the unit tests; this is the empirical A/B.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from app.retrieval import context_retriever as cr
from app.retrieval.embeddings import embed_texts
from app.retrieval.note_extractor import (DEFAULT_TRANSCRIPT_DIR, _load_transcripts,
                                          derive_allowed_scopes, extract_notes_df)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Seeded probes — (label, finding, expected substring the correct note should carry). The findings mirror
# the demo's hero CPA spike and the fallout channel; the expected token is the ground-truth cause.
_PROBES = [
    ("CPA spike — ERCOT/North/Door_to_Door",
     {"entity": "ERCOT", "region": "North", "segment": "Door_to_Door",
      "metric": "cost_per_acquisition", "alert_type": "cpa_spike"},
     "commission"),
    ("Fallout — ERCOT/South/Telemarketing",
     {"entity": "ERCOT", "region": "South", "segment": "Telemarketing",
      "metric": "fallout_rate", "alert_type": "fallout_rate"},
     "turnover"),
]


def _hit(notes: list[dict], expected: str) -> bool:
    """Did the correct cause surface anywhere in the retrieved set?"""
    return any(expected.lower() in n["note_text"].lower() for n in notes)


def _run(snapshot_date: dt.date, top_k: int = 3) -> int:
    """Extract notes from the synthetic transcripts, then compare filter vs semantic on each probe."""
    from app.analytics.data_loader import load_data
    from app.llm.llm_client import call_llm

    # Extract the real (messy-input) notes once; both arms retrieve over the same corpus.
    data = load_data()
    allowed = derive_allowed_scopes(data["reference_data"])
    transcripts = _load_transcripts(DEFAULT_TRANSCRIPT_DIR)
    notes = extract_notes_df(transcripts, client=call_llm, allowed_scopes=allowed)
    notes["date"] = pd.to_datetime(notes["date"])
    print(f"\nExtracted {len(notes)} note row(s) from {len(transcripts)} transcript(s):")
    for _, n in notes.iterrows():
        print(f"  {n['date'].date()}  {n['entity']}/{n['region']}/{n['segment']}: {n['note_text'][:80]}")

    # Head-to-head: for each probe, does each strategy surface the ground-truth cause in top_k?
    print(f"\n{'probe':<44}{'filter':<10}{'semantic':<10}")
    print("-" * 64)
    filter_hits = semantic_hits = 0
    for label, finding, expected in _PROBES:
        f_notes = cr.retrieve_notes(finding, notes, snapshot_date=snapshot_date, top_k=top_k,
                                    strategy="filter")
        s_notes = cr.retrieve_notes(finding, notes, snapshot_date=snapshot_date, top_k=top_k,
                                    strategy="semantic", embedder=embed_texts)
        f_ok, s_ok = _hit(f_notes, expected), _hit(s_notes, expected)
        filter_hits += f_ok
        semantic_hits += s_ok
        print(f"{label:<44}{'HIT' if f_ok else 'miss':<10}{'HIT' if s_ok else 'miss':<10}")

    print("-" * 64)
    print(f"{'TOTAL':<44}{f'{filter_hits}/{len(_PROBES)}':<10}{f'{semantic_hits}/{len(_PROBES)}':<10}")
    print("\nRecord the winner (and any quality difference) in docs/decisions_log.md (BS8); keep "
          "strategy='filter' the default unless semantic clearly wins.")
    return 0


def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config" / "system_config.yaml").read_text())
    snapshot_date = dt.date.fromisoformat(str(cfg["snapshot_date"]))
    return _run(snapshot_date)


if __name__ == "__main__":
    raise SystemExit(main())
