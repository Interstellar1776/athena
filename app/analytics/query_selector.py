#!/usr/bin/env python3
"""query_selector.py — pick the slice of the already-computed results a question is about (Mode 2).

Build Sequence §19 step 9 (context doc §5). Between the router (question → `QuerySpec`) and the answer,
this is the pure, deterministic *select* step: given a validated spec and the rich `run_pipeline` result,
return the record(s) to answer from. No LLM, no I/O, no math — the numbers were all computed upstream and
carry their method labels; this only chooses which ones the question points at.

Two intents, two sources (both already in the result):

* **explain** → the ranked §14 **findings** (`result["findings"]`) — the flagged conditions, each
  already grounded-ready and number-safe. Filtered by the spec's scope + metric.
* **lookup** → the **assessments** frame (`result["assessments"]`), which scores *every* channel/metric
  including the calm ones — so "what's CPA for a quiet channel?" has an honest answer where `findings`
  (flagged-only) has none. The chosen assessment row is shaped into a finding-like dict so the same
  number-safe formatter (`placeholders.render_placeholder`) and `context_retriever` consume it unchanged.

The method label rides along untouched (`actual_method`, `estimated`), so the orchestrator can be honest
about estimates and unresolved metrics (§17 worked example: "…LTV couldn't complete, retention isn't
configured for that segment").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from app.analytics.findings_builder import FLAGGED, SEVERITY_RANK
from app.analytics.metrics_calculator import _snapshot_period

logger = logging.getLogger(__name__)

SCOPE_DIMS = ("entity", "region", "segment")

# The assessment columns that map onto the finding shape the number-safe formatter reads.
_FINDING_KEYS = ("entity", "region", "segment", "period", "alert_type", "metric", "actual",
                 "actual_method", "reference_value", "reference_type", "variance_pct",
                 "variance_direction", "risk_level", "estimated", "confidence")


@dataclass
class SelectionResult:
    """What the question resolved to. ``records`` are finding-like dicts (empty if nothing matched);
    ``kind`` echoes the intent; ``calm`` is True for a lookup whose best match carries no alert."""

    kind: str
    records: list[dict]
    calm: bool = False


# ===========================================================================
# 1. Scope matching — a record matches iff every SPECIFIED dim/metric agrees
# ===========================================================================
def _scope_filter(df: pd.DataFrame, spec) -> pd.DataFrame:
    """Keep rows matching every dim the spec pins down (an unspecified dim is a wildcard)."""
    mask = pd.Series(True, index=df.index)
    for dim in SCOPE_DIMS:
        val = getattr(spec, dim)
        if val is not None:
            mask &= df[dim] == val
    if spec.metric is not None:
        mask &= df["metric"] == spec.metric
    return df[mask]


def _row_to_finding(row: pd.Series) -> dict:
    """Shape one assessment row into a finding-like dict (supporting dict → supporting_metrics)."""
    rec = {k: row[k] for k in _FINDING_KEYS if k in row}
    rec["supporting_metrics"] = row.get("supporting") or {}
    rec["retrieved_context"] = ""                            # filled by context_retriever downstream
    return rec


# ===========================================================================
# 2. Select — explain from findings, lookup from assessments
# ===========================================================================
def _select_explain(spec, result: dict) -> SelectionResult:
    """The flagged findings matching the spec's scope + metric, most-severe first (already ranked)."""
    matches = [f for f in result["findings"]
               if all(getattr(spec, d) is None or f.get(d) == getattr(spec, d) for d in SCOPE_DIMS)
               and (spec.metric is None or f.get("metric") == spec.metric)]
    return SelectionResult(kind="explain", records=matches)


def _select_lookup(spec, result: dict) -> SelectionResult:
    """The current-period value for the asked channel/metric — from assessments, so calm channels answer
    too. Picks the most-severe representative row (with a usable value) per the spec's scope+metric."""
    assessments = result["assessments"]
    current = _snapshot_period(result["snapshot_date"])
    scoped = _scope_filter(assessments[assessments["period"] == current], spec)
    if scoped.empty:
        return SelectionResult(kind="lookup", records=[])

    # Rank by severity, then prefer a row that actually carries a reference to compare against.
    scoped = scoped.assign(_sev=scoped["risk_level"].map(SEVERITY_RANK),
                           _has_ref=scoped["reference_value"].notna())
    scoped = scoped.sort_values(["_sev", "_has_ref"], ascending=[True, False])
    best = scoped.iloc[0]
    return SelectionResult(kind="lookup", records=[_row_to_finding(best)],
                           calm=best["risk_level"] not in FLAGGED)


def select(spec, result: dict) -> SelectionResult:
    """Resolve a validated ``QuerySpec`` to the record(s) to answer from (pure over the result)."""
    out = _select_explain(spec, result) if spec.intent == "explain" else _select_lookup(spec, result)
    logger.info("query_selector: intent=%s → %d record(s)%s",
                spec.intent, len(out.records), " (calm)" if out.calm else "")
    return out
