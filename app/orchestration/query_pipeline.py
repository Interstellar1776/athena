#!/usr/bin/env python3
"""query_pipeline.py — the on-demand conversational run (Mode 2's orchestrator).

Build Sequence §19 step 9 (context doc §5 Mode 2, §17). The pull counterpart to `batch_pipeline`: a
natural-language question in, a grounded, number-safe answer out. It owns the coordination and the §17
plain-language handling; the real work is delegated to the pure cores, and the number-safety spine is
**reused wholesale**:

    question
      → run_pipeline (analytics, once)         reuse — every metric already computed + labeled
      → query_router.route  (LLM → QuerySpec)  understand the question, bounded to the vocabulary
      → query_selector.select                  pick the finding(s)/assessment the question points at
      → context_retriever.attach_context       reuse — ground the record(s) in notes + GL (step 7)
      → batch_pipeline.narrate_findings         reuse — generate→validate→retry→number-safe fallback
      → structured answer dict {question, query_spec, status, answer, matched}

Because synthesis goes through `narrate_findings`, the LLM never types a digit (§4) in a chat answer any
more than in the proactive feed — the validator is in the loop, and a calm lookup gets the deterministic
number-safe data block (no LLM needed to state a value).

**§17 — conversational failures degrade gracefully, never a blank or a stack trace.** Unlike the batch
pipeline (unattended → halt loudly), a chat turn always returns a plain-language answer: an unclear
question asks for a rephrase; an off-vocabulary filter names what we *do* track; a calm/absent metric
says so; an unexpected error is logged loudly but the user sees a civil message, not a traceback.

CLI:
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.orchestration.query_pipeline "Why is CPA up in ERCOT North?"
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable

from app.analytics.query_selector import SCOPE_DIMS, select
from app.analytics.variance_engine import run_pipeline
from app.llm.llm_client import call_llm
from app.llm.narrative_generator import DEFAULT_AUDIENCE, DEFAULT_NUMBERS, DEFAULT_STYLE
from app.llm.query_router import RouterError, route
from app.orchestration.batch_pipeline import narrate_findings
from app.retrieval.context_retriever import attach_context

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

_DEFAULT_KNOBS = {"numbers": DEFAULT_NUMBERS, "style": DEFAULT_STYLE, "audience": DEFAULT_AUDIENCE}


# ===========================================================================
# 1. Vocabulary + plain-language helpers
# ===========================================================================
def _build_vocab(result: dict) -> dict[str, Any]:
    """The router's menu, read live from the computed assessments (never hardcoded): the real metric
    names and the actual entity/region/segment values (no note-style ``ALL`` — channels are concrete)."""
    a = result["assessments"]
    return {"metrics": sorted(a["metric"].unique()),
            **{dim: sorted(a[dim].unique()) for dim in SCOPE_DIMS}}


def _scope_phrase(spec) -> str:
    """A human phrase for what the question scoped to, e.g. 'CPA in ERCOT North Door_to_Door'."""
    where = " ".join(str(getattr(spec, d)) for d in SCOPE_DIMS if getattr(spec, d) is not None)
    metric = spec.metric or "that metric"
    return f"{metric}{(' in ' + where) if where else ''}"


def _clarify_unclear() -> str:
    return ("I couldn't tell what you're asking. Try naming a metric and a channel — for example, "
            "\"Why is CPA up in ERCOT North?\" or \"What's fallout for PJM East?\"")


def _clarify_unresolved(spec, vocab: dict) -> str:
    """Name what we DO track for each off-vocabulary value the router surfaced (§17)."""
    lines = []
    for field, val in spec.unresolved:
        known = vocab.get(field if field != "metric" else "metrics", [])
        lines.append(f"I don't track \"{val}\" as a {field}. I do track: {', '.join(map(str, known))}.")
    return " ".join(lines)


def _no_match_text(spec) -> str:
    if spec.intent == "explain":
        return (f"Nothing is currently flagged for {_scope_phrase(spec)} — it looks within plan for this "
                "period. Ask a \"what's …\" question to see the current value.")
    return (f"I don't have data for {_scope_phrase(spec)} in the current period. "
            "Check the channel/metric names and try again.")


def _answer(question: str, spec, *, status: str, text: str, matched: list[dict] | None = None) -> dict:
    """Assemble the UI-ready answer envelope (mirrors report_generator's structured output)."""
    return {
        "question": question,
        "query_spec": (None if spec is None else
                       {"intent": spec.intent, "metric": spec.metric, "entity": spec.entity,
                        "region": spec.region, "segment": spec.segment,
                        "reference_type": spec.reference_type, "unresolved": spec.unresolved}),
        "status": status,                                    # ok | unclear | unresolved | no_match | error
        "answer": text,
        "matched": [{"finding_id": m.get("finding_id"), "metric": m.get("metric"),
                     "risk_level": m.get("risk_level"), "estimated": m.get("estimated"),
                     "narrative_filled": m.get("narrative_filled")}
                    for m in (matched or [])],
    }


# ===========================================================================
# 2. The conversational run — the single on-demand entry point
# ===========================================================================
def answer_query(question: str, *, config_path: Path = DEFAULT_SYSTEM_CONFIG,
                 client: Callable[..., str] = call_llm, result: dict | None = None,
                 knobs: dict | None = None) -> dict:
    """Answer one natural-language question and return the structured answer envelope.

    ``result`` may be injected (a pre-run analytics result) so tests/UI skip re-running the pipeline;
    otherwise it is computed once from ``config_path``. Any failure degrades to a plain-language answer
    (§17) — the operator sees the loud log, the user never sees a traceback.
    """
    knobs = knobs or _DEFAULT_KNOBS
    spec = None
    try:
        if result is None:
            result = run_pipeline(config_path)
        vocab = _build_vocab(result)
        spec = route(question, client=client, vocab=vocab)

        # §17 — the expected conversational off-ramps, each a helpful plain-language reply.
        if spec.intent is None:
            return _answer(question, spec, status="unclear", text=_clarify_unclear())
        if spec.unresolved:
            return _answer(question, spec, status="unresolved", text=_clarify_unresolved(spec, vocab))

        selection = select(spec, result)
        if not selection.records:
            return _answer(question, spec, status="no_match", text=_no_match_text(spec))

        # Ground the selected record(s), then synthesize through the shared number-safe spine.
        grounded = attach_context(selection.records, result["operational_notes"], result["gl_mapping"],
                                  snapshot_date=result["snapshot_date"])
        narrated = narrate_findings(grounded, client=client, knobs=knobs)
        return _answer(question, spec, status="ok", text=narrated[0]["narrative_filled"], matched=narrated)

    except RouterError as exc:
        logger.warning("query_pipeline: router failed on %r: %s", question, exc)
        return _answer(question, spec, status="error",
                       text="I had trouble understanding that question. Please try rephrasing it.")
    except Exception as exc:                                  # noqa: BLE001 — never a stack trace to the user (§17)
        logger.exception("query_pipeline: unexpected failure on %r", question)
        return _answer(question, spec, status="error",
                       text=f"I ran into a problem answering that ({type(exc).__name__}). "
                            "The details were logged; please try again.")


# ===========================================================================
# 3. CLI — ask a question against the configured snapshot
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Ask Athena a question about the configured snapshot.")
    ap.add_argument("question", nargs="+", help="the natural-language question")
    args = ap.parse_args()

    ans = answer_query(" ".join(args.question))
    print(f"\nQ: {ans['question']}")
    print(f"[{ans['status']}] {ans['answer']}")
    if ans["matched"]:
        print("\nmatched:")
        for m in ans["matched"]:
            print(f"  {m['finding_id']}  {m['metric']}  {m['risk_level']}  est={m['estimated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
