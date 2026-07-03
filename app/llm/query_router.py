#!/usr/bin/env python3
"""query_router.py — a natural-language question → a validated, structured query spec (Mode 2).

Build Sequence §19 step 9 (context doc §5 Mode 2). This is the *understand* half of the conversational
interface: it turns "Why is acquisition cost up in ERCOT North?" into a small, fully-bounded `QuerySpec`
that Python can act on. It never touches numbers or computes anything — its one job is language→intent.

Design (decisions in `docs/decisions_log.md` BS9):

* **Constrained query-spec, not free-form.** The LLM emits a JSON object whose every field is drawn from
  an enumerated **vocabulary** (the intents, the real metric names, the actual entity/region/segment
  dimensions) that we hand it in the prompt. Python then validates each field against that same
  vocabulary. Safe like a fixed menu, flexible enough for real questions — the same LLM+JSON+validate
  shape `note_extractor` uses.
* **Off-vocabulary values are surfaced, not silently dropped.** If the model answers `entity="Midwest"`
  (a market we don't track), that lands in `unresolved` so the pipeline can say so in plain language
  (§17), rather than pretending the filter didn't exist.
* **Two failure modes, kept distinct.** A *malformed* reply (no parseable JSON) fails loud
  (`RouterError`, the `_stage` pattern, §17). An *unrecognizable* question — valid JSON but no usable
  intent — yields `intent=None`, which the pipeline turns into a friendly clarification, not an error.

Provider-agnostic, env-driven LLM via the injected `call_llm`-shaped client — never a hardcoded model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

INTENTS = ("explain", "lookup")
SCOPE_DIMS = ("entity", "region", "segment")
REFERENCE_TYPES = ("plan", "forecast")

# Short glosses shown to the model so it maps synonyms ("acquisition cost", "CPA") onto the canonical
# metric name. The canonical set itself is passed in from the live data (never hardcoded here).
_METRIC_GLOSS = {
    "cost_per_acquisition": "CPA / acquisition cost / cost per sale",
    "volume_converted": "conversions / activations / sales volume",
    "fallout_rate": "fallout / drop-off / non-conversion rate",
    "margin_per_unit": "margin / contribution per unit",
    "cogs_per_unit": "COGS / cost of goods per unit",
    "t3m_cpa_over_ltv": "CPA-vs-LTV compression (trailing 3-month)",
    "t12m_cpa_over_ltv": "CPA-vs-LTV inversion (trailing 12-month)",
}


class RouterError(RuntimeError):
    """The router failed on a malformed model reply — carries context for a loud, actionable halt (§17)."""


@dataclass
class QuerySpec:
    """The bounded, validated shape of a question. Every field is either a known-vocabulary value or None.

    ``intent=None`` means "couldn't understand"; ``unresolved`` lists ``(field, value)`` pairs the model
    proposed that aren't in the vocabulary (so the pipeline can name what we DO track)."""

    question: str
    intent: str | None
    metric: str | None = None
    entity: str | None = None
    region: str | None = None
    segment: str | None = None
    reference_type: str | None = None
    unresolved: list[tuple[str, str]] = field(default_factory=list)


# ===========================================================================
# 1. Prompt — hand the model the vocabulary and demand JSON drawn only from it
# ===========================================================================
def build_router_prompt(question: str, *, vocab: dict[str, Any]) -> tuple[str, str]:
    """Build the (system, user) prompt. ``vocab`` carries ``metrics`` (canonical names) and the
    ``entity``/``region``/``segment`` value sets — the only values the model may choose from."""
    system = (
        "You translate a user's question about operational metrics into a small JSON object for an "
        "analytics system. You do not answer the question or state any number — you only classify it.\n\n"
        "OUTPUT: a single JSON object only (no prose, no markdown fence) with these keys:\n"
        '  "intent"  — one of: "explain" (why is a metric off / what\'s driving it) or '
        '"lookup" (what is the current value / how is it doing). null if the question is neither.\n'
        '  "metric"  — the metric asked about, chosen ONLY from the allowed metrics, or null.\n'
        '  "entity", "region", "segment" — the channel/geography, chosen ONLY from the allowed values, '
        "or null if not specified.\n"
        '  "reference_type" — "plan" or "forecast" if the user names one, else null.\n\n'
        "RULES: choose values only from the allowed lists, spelled exactly. Map synonyms to the allowed "
        "name (e.g. 'acquisition cost' → the CPA metric). Use null for anything the question doesn't "
        "specify. Never invent a value that isn't in the lists."
    )

    metric_lines = "\n".join(f"    {m} — {_METRIC_GLOSS.get(m, m)}" for m in vocab.get("metrics", []))
    user = (
        "ALLOWED METRICS:\n"
        f"{metric_lines}\n\n"
        "ALLOWED CHANNEL/GEOGRAPHY:\n"
        f"    entity:  {sorted(vocab.get('entity', []))}\n"
        f"    region:  {sorted(vocab.get('region', []))}\n"
        f"    segment: {sorted(vocab.get('segment', []))}\n\n"
        f"QUESTION: {question}\n\n"
        "Return the JSON object now."
    )
    return system, user


# ===========================================================================
# 2. Parse + validate — every field must trace to the vocabulary
# ===========================================================================
def _parse_json_object(reply: str) -> dict:
    """Pull the JSON object out of the model reply (tolerant of stray prose / fences). A reply with no
    parseable object fails loud — the model didn't honor the contract."""
    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RouterError(f"no JSON object found in router reply: {reply[:200]!r}")
    try:
        parsed = json.loads(reply[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RouterError(f"router reply is not valid JSON: {exc}; reply={reply[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise RouterError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _validate(question: str, raw: dict, vocab: dict[str, Any]) -> QuerySpec:
    """Coerce a raw model object into a QuerySpec, validating each field against the vocabulary.

    Anything off-vocabulary is recorded in ``unresolved`` (never silently kept); unspecified stays None."""
    unresolved: list[tuple[str, str]] = []

    def _check(fieldname: str, allowed) -> str | None:
        val = raw.get(fieldname)
        if val in (None, "", "null"):
            return None
        if str(val) in allowed:
            return str(val)
        unresolved.append((fieldname, str(val)))            # proposed but not tracked — surface it
        return None

    intent = raw.get("intent")
    intent = intent if intent in INTENTS else None          # unknown/None intent → "couldn't understand"

    return QuerySpec(
        question=question, intent=intent,
        metric=_check("metric", set(vocab.get("metrics", []))),
        entity=_check("entity", set(vocab.get("entity", []))),
        region=_check("region", set(vocab.get("region", []))),
        segment=_check("segment", set(vocab.get("segment", []))),
        reference_type=_check("reference_type", set(REFERENCE_TYPES)),
        unresolved=unresolved,
    )


def route(question: str, *, client: Callable[..., str], vocab: dict[str, Any],
          model: str | None = None) -> QuerySpec:
    """Turn ``question`` into a validated ``QuerySpec``. ``client`` is a ``call_llm``-shaped callable —
    injected so tests stub it without a network/SDK."""
    system, user = build_router_prompt(question, vocab=vocab)
    try:
        reply = client(system, user, model=model)
    except Exception as exc:                                  # noqa: BLE001 — fail loud, with context (§17)
        raise RouterError(f"query_router: LLM call failed: {exc}") from exc
    spec = _validate(question, _parse_json_object(reply), vocab)
    logger.info("query_router: intent=%s metric=%s scope=%s/%s/%s unresolved=%s",
                spec.intent, spec.metric, spec.entity, spec.region, spec.segment, spec.unresolved)
    return spec
