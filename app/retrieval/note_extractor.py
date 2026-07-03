#!/usr/bin/env python3
"""note_extractor.py — raw transcripts → structured operational-note rows (Build Sequence §19 step 8).

`operational_notes.csv` has been a hand-authored, perfectly-tagged **stand-in for post-extraction
output** (context doc §13). Step 7 (`context_retriever`) was proven against that stand-in; this module
now produces those rows *for real* from messy, untagged meeting transcripts, using the LLM for
**language→structure only**. The output matches the `operational_notes` schema exactly
(`date, entity, region, segment, note_text, author`, `"ALL"` = per-level wildcard), so
`context_retriever` consumes it unchanged.

Design (decisions in `docs/decisions_log.md` BS8; the spine in `docs/the_hallucination_guard.md`):

* **The LLM assigns tags; Python guards structure.** The valid-dimension *dictionary* (derived from a
  loaded dataframe via `derive_allowed_scopes`) is fed into the prompt so the model tags against real
  values. Any row whose `(entity, region, segment)` still isn't a known dimension is **discarded**
  (logged, never silent) — a mis-tagged note can't be routed, so it doesn't reach retrieval.
* **Numbers stay verbatim context, never metrics (§4).** The schema has no numeric field, so a number
  can only land in `note_text`. A **provenance check** enforces the spine: every numeric run in an
  extracted `note_text` must appear **verbatim** in the source transcript (exact-substring, the same
  rule `narrative_validator` applies to `retrieved_context`). A note carrying an *ungrounded* number is
  **discarded** — otherwise a hallucinated number would ride into `retrieved_context`, where the
  validator would then wrongly *allow* it as "grounded." Discarding here protects §4 end-to-end.
* **Provider-agnostic, env-driven LLM** via the injected `call_llm`-shaped client (`app/llm/llm_client`)
  — never a hardcoded model.
* **Fail loud with stage context (§17)** — the `ExtractionError`/`_stage` pattern mirrors
  `variance_engine`. A *malformed* model response (unparseable JSON) halts loudly; a *well-formed but
  invalid* row is discarded (bad structure vs. bad content).

CLI:
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.retrieval.note_extractor
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"
DEFAULT_TRANSCRIPT_DIR = REPO_ROOT / "data" / "contextual" / "transcripts"

# The operational_notes schema (context doc §13) — the exact columns/order context_retriever consumes.
NOTE_COLUMNS = ("date", "entity", "region", "segment", "note_text", "author")
SCOPE_DIMS = ("entity", "region", "segment")
WILDCARD = "ALL"

# A numeric run in prose: an integer with optional thousands-commas and an optional decimal — so
# "9,800" and "9.8" each count once. Matches narrative_validator's rule so provenance is consistent.
_NUMERIC_RE = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")


class ExtractionError(RuntimeError):
    """A stage of note extraction failed — carries which stage, for a loud, actionable halt (§17)."""


def _stage(name: str, fn: Callable, *args, **kwargs) -> Any:
    """Run one extraction stage; re-raise any failure naming the stage (§17)."""
    try:
        return fn(*args, **kwargs)
    except ExtractionError:
        raise
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with context
        raise ExtractionError(f"note_extractor: stage '{name}' failed: {exc}") from exc


# ===========================================================================
# 1. The dimension dictionary — derived from a loaded dataframe
# ===========================================================================
def derive_allowed_scopes(df: pd.DataFrame) -> dict[str, set[str]]:
    """The per-level valid-value dictionary the LLM tags against, read from any frame carrying the
    scope dims (`reference_data` is the canonical roster; `gl_mapping`/notes also work).

    Returned as ``{dim: {values…}}`` with the ``"ALL"`` wildcard always permitted. Per-level (not
    per-combination) to match `context_retriever`, which itself matches each dim independently."""
    missing = [d for d in SCOPE_DIMS if d not in df.columns]
    if missing:
        raise ExtractionError(f"derive_allowed_scopes: frame lacks scope dims {missing} "
                              f"(has {list(df.columns)})")
    return {d: {WILDCARD, *(str(v) for v in df[d].dropna().unique())} for d in SCOPE_DIMS}


def _scope_is_valid(row: dict, allowed: dict[str, set[str]]) -> bool:
    """Every scope dim must be a known value for its level (``"ALL"`` always allowed)."""
    return all(str(row.get(d)) in allowed[d] for d in SCOPE_DIMS)


# ===========================================================================
# 2. Prompt construction — the LLM turns language into tagged structure
# ===========================================================================
def build_extraction_prompt(transcript: str, *, allowed_scopes: dict[str, set[str]]) -> tuple[str, str]:
    """Build the (system, user) prompt for one transcript.

    System = the standing rules (emit JSON rows; tag only from the provided dictionary; numbers stay
    verbatim in `note_text` only). User = the dimension dictionary + the raw transcript.
    """
    system = (
        "You convert a raw meeting transcript into structured operational-note rows for an analytics "
        "system. Extract each distinct operational point a note-taker would record.\n\n"
        "OUTPUT: a JSON array only (no prose, no markdown fence). Each element is an object with EXACTLY "
        "these keys:\n"
        '  "date"     — ISO date (YYYY-MM-DD) the point refers to, taken from the transcript.\n'
        '  "entity", "region", "segment" — tags chosen ONLY from the ALLOWED VALUES below. Use "ALL" '
        "for any level the point is not specific to (an org-wide or cross-channel note).\n"
        '  "note_text" — one or two sentences stating the key operational point.\n'
        '  "author"   — the speaker or their role if identifiable, else "Meeting".\n\n'
        "ABSOLUTE RULE — NUMBERS: never compute, round, estimate, or invent a number. If you mention a "
        "number, copy it VERBATIM from the transcript, and only inside `note_text` — never in any other "
        "field. Numbers are context (a quoted figure, a date), never a calculated metric.\n\n"
        "TAGS: use only the allowed values, spelled exactly. If unsure of a level, use \"ALL\"."
    )

    dims = "\n".join(f"  {d}: {sorted(v for v in allowed_scopes[d] if v != WILDCARD)}  (or \"ALL\")"
                     for d in SCOPE_DIMS)
    user = (
        "ALLOWED VALUES (tag only from these):\n"
        f"{dims}\n\n"
        "TRANSCRIPT:\n"
        f"{transcript}\n\n"
        "Return the JSON array now."
    )
    return system, user


# ===========================================================================
# 3. Parsing + validation — structure loud, content discarded
# ===========================================================================
def _parse_json_array(reply: str) -> list[dict]:
    """Pull the JSON array of row objects out of the model reply (tolerant of stray prose / fences).

    A *malformed* reply (no parseable array) is a loud failure — the model didn't honor the contract."""
    text = reply.strip()
    # Isolate the outermost [...] span so a stray sentence or ```json fence around it doesn't break parsing.
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ExtractionError(f"no JSON array found in model reply: {reply[:200]!r}")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model reply is not valid JSON: {exc}; reply={reply[:200]!r}") from exc
    if not isinstance(parsed, list):
        raise ExtractionError(f"expected a JSON array, got {type(parsed).__name__}")
    return parsed


def _numbers_are_grounded(note_text: str, transcript: str) -> bool:
    """Every numeric run in `note_text` must appear verbatim in the transcript (§4 provenance)."""
    return all(run in transcript for run in _NUMERIC_RE.findall(note_text))


def _clean_row(raw: dict, *, allowed_scopes: dict[str, set[str]], transcript: str) -> dict | None:
    """Validate one model row into a note dict, or return ``None`` (discard, with reason logged).

    Discards — never silently — on: missing keys, unparseable date, invalid scope tag, or an ungrounded
    number. All four are *content* problems with a single bad row, not a broken contract, so we drop the
    row and keep extracting rather than halting the batch."""
    if not isinstance(raw, dict) or any(k not in raw for k in NOTE_COLUMNS):
        logger.warning("note_extractor: discarding row — missing keys: %.120s", str(raw))
        return None

    # Date must parse to a real ISO date.
    try:
        date = dt.date.fromisoformat(str(raw["date"]).strip())
    except ValueError:
        logger.warning("note_extractor: discarding row — bad date %r", raw.get("date"))
        return None

    # Tags must be known dimensions (the LLM was given the dictionary; enforce it).
    if not _scope_is_valid(raw, allowed_scopes):
        logger.warning("note_extractor: discarding row — invalid scope (%s/%s/%s)",
                       raw.get("entity"), raw.get("region"), raw.get("segment"))
        return None

    # Numbers must trace verbatim to the transcript, or the note carries a hallucinated figure.
    note_text = str(raw["note_text"]).strip()
    if not _numbers_are_grounded(note_text, transcript):
        logger.warning("note_extractor: discarding row — ungrounded number in note: %.120s", note_text)
        return None

    return {"date": date.isoformat(), "entity": str(raw["entity"]), "region": str(raw["region"]),
            "segment": str(raw["segment"]), "note_text": note_text,
            "author": str(raw["author"]).strip() or "Meeting"}


# ===========================================================================
# 4. Public surface — transcript(s) → note rows / dataframe
# ===========================================================================
def extract_notes(transcript: str, *, client: Callable[..., str], allowed_scopes: dict[str, set[str]],
                  model: str | None = None) -> list[dict]:
    """Extract operational-note rows from one transcript. ``client`` is a ``call_llm``-shaped callable
    ``(system, user, *, model) -> str`` — injected so tests stub it without a network/SDK."""
    system, user = build_extraction_prompt(transcript, allowed_scopes=allowed_scopes)
    reply = _stage("llm_call", client, system, user, model=model)
    rows = _stage("parse", _parse_json_array, reply)

    kept = [row for raw in rows
            if (row := _clean_row(raw, allowed_scopes=allowed_scopes, transcript=transcript)) is not None]
    logger.info("note_extractor: kept %d/%d extracted row(s)", len(kept), len(rows))
    return kept


def extract_notes_df(transcripts: list[str], *, client: Callable[..., str],
                     allowed_scopes: dict[str, set[str]], model: str | None = None) -> pd.DataFrame:
    """Extract from one or more transcripts into a single `operational_notes`-schema dataframe (exact
    column order), ready for `context_retriever` to consume unchanged."""
    rows: list[dict] = []
    for transcript in transcripts:
        rows.extend(extract_notes(transcript, client=client, allowed_scopes=allowed_scopes, model=model))
    return pd.DataFrame(rows, columns=list(NOTE_COLUMNS))


# ===========================================================================
# 5. End-to-end wrapper + CLI — load transcripts, derive scopes, call the LLM
# ===========================================================================
def _load_transcripts(transcript_dir: Path) -> list[str]:
    """Read every ``*.txt`` transcript in a directory (sorted, for determinism)."""
    if not transcript_dir.is_dir():
        raise ExtractionError(f"transcript directory not found: {transcript_dir} — generate synthetic "
                              "transcripts first (scripts/generators/gen_transcripts.py).")
    paths = sorted(transcript_dir.glob("*.txt"))
    if not paths:
        raise ExtractionError(f"no *.txt transcripts in {transcript_dir}")
    return [p.read_text() for p in paths]


def generate(config_path: Path = DEFAULT_SYSTEM_CONFIG,
             transcript_dir: Path = DEFAULT_TRANSCRIPT_DIR) -> pd.DataFrame:
    """End-to-end: load transcripts, derive the scope dictionary from the pipeline's `reference_data`,
    call the configured LLM, return the extracted notes frame."""
    from app.analytics.data_loader import load_data
    from app.llm.llm_client import call_llm

    data = load_data(config_path)                            # load + gate + clean (halts on bad data)
    allowed = derive_allowed_scopes(data["reference_data"])
    transcripts = _load_transcripts(transcript_dir)
    return extract_notes_df(transcripts, client=call_llm, allowed_scopes=allowed)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Extract operational-note rows from raw transcripts.")
    ap.add_argument("--transcripts", type=Path, default=DEFAULT_TRANSCRIPT_DIR,
                    help="directory of *.txt transcripts to extract from")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the notes CSV here instead of printing (operational_notes schema)")
    args = ap.parse_args()

    notes = generate(transcript_dir=args.transcripts)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        notes.to_csv(args.out, index=False)
        print(f"wrote {len(notes)} note row(s) to {args.out}")
    else:
        print(f"\n=== extracted {len(notes)} operational-note row(s) ===")
        for _, n in notes.iterrows():
            print(f"  {n['date']}  {n['entity']}/{n['region']}/{n['segment']}: {n['note_text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
