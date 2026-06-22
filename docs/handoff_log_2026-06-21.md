# Athena — Handoff: Analytics Core complete (Build Sequence 3)

> **Audience:** the next engineer/LLM picking up Athena. This is a point-in-time status snapshot.
> The authoritative docs are `athena_context.md` (architecture, **[LOCKED]** decisions),
> `decisions_log.md` (why), `data_dictionary.md` (data contract), `build_log.md` (what shipped),
> `open_questions.md` (unresolved). Read `athena_context.md` first; this just orients you fast.

## What Athena is (one paragraph)
A proactive operational-intelligence platform. **Python computes every number; the LLM only interprets.**
A deterministic analytics core turns operational data into ranked, method-labeled "findings" before
period close; an LLM layer (not yet built) writes the prose by referencing **placeholder tokens Python
fills** — it never emits a numeral (the §4 hallucination guard). Reference domain: retail energy, but
the architecture is industry-agnostic.

## Where we are
**Build Sequence 3 (the analytics core) is COMPLETE and merged to `main`** (PR #5). `122` tests pass.
The whole deterministic pipeline runs from one entry and produces the §14 findings feed.

Done: BS1 (synthetic data + config), BS2 (ingestion validator), **BS3 (analytics core)**.
Not started: BS4 narrative generation → BS5 narrative validation → BS6 batch pipeline + reporting →
BS7 retrieval → BS8 conversational query → BS9 web UI.

## The pipeline (all in `app/analytics/`, runs in this order)
```
data_loader.load_data(config)        # read → INGESTION GATE (halt on bad data) → type/clean → dict of frames
  → data_merger.merge_frames         # aggregate-then-join facts↔reference↔GL to leaf×period (no row explosion)
  → gl_processor.gl_completeness     # GL state open/closed/restated/accrued + late-invoice flags (by document month)
  → metrics_calculator.calculate_metrics   # CPA(unit) + COGS/margin/LTV/fallout(leaf), each METHOD-LABELED
  → projection_engine.project_volume + project_fallout  # period-end volume proj + proactive fallout (current period)
  → risk_classifier.classify         # score EVERY metric×period×grain HIGH/MED/LOW/INFO over a 6-mo window
  → findings_builder.build_findings   # roll non-LOW into ranked §14 findings (unit headline, leaf drill-down)
```
**`variance_engine.run_pipeline(config)`** is the single entry that threads all the above **once** and
returns a rich result: `{findings, assessments, metrics, projection, gl_states, merged, summary}`.
(Each module also has a `compute_*` wrapper that re-runs the chain — handy for module CLIs/tests; the
orchestrator is the efficient production path.)

Run it: `python -m app.analytics.variance_engine` (or `from app.analytics.variance_engine import run_pipeline`).

## Spine principles (LOCKED — do not violate)
1. **Python owns every number; the LLM references placeholder tokens.** (§3, §4)
2. **Every metric carries a method label** (real / estimated / plan-derived); incomplete data →
   labeled estimate, never a blank. (§9–§10)
3. **Bad ingestion data halts loudly** at the gate; never analyze unvalidated data. (§17)
4. **Severity = magnitude only; `estimated` is orthogonal** — an estimated HIGH stays HIGH; low
   confidence is shown, never suppressed. (§6, §11)
5. **Findings grain is metric-driven:** CPA & CPA-vs-LTV at **unit** (entity,region,segment) because GL
   resolves only there; COGS/margin/fallout/volume at **leaf** (full 8-dim hierarchy). (§14)

## The §14 structured finding (the system contract)
`findings_builder` emits a list of dicts; downstream (narrative, report) read/write this and invent
nothing outside it. Key fields: `finding_id`, dims, `metric`, `period`, `risk_level`, `estimated`,
`confidence`, `actual`+`actual_method`, `reference_value`+`reference_type`, `variance_pct`/direction,
`projected_period_end_linear/_weighted` (volume), economic context (`cogs/ltv/margin` + methods),
`unit_economics_flag`, `gl_completeness_state`, `frozen_reference`/`restatement_delta`,
`supporting_metrics` (drill-down incl. `leaves` + `exceedance`), and empty downstream slots
(`retrieved_context`, `narrative`, `validated`, `validation_flags`). Full spec: `athena_context.md` §14.
Alongside the findings (the feed) is the full **assessment table** — every metric scored incl. LOW —
the browse/drill-down layer.

## Key analytics decisions made during BS3 (rationale in `decisions_log.md`)
- **CPA is unit-grain**, GL-resolved; method hierarchy `real → gl_partial (open, period-to-date) →
  trailing_avg → plan_input`. CPA is **not projected** (ledger/invoice-paced); its finding pairs the
  spend-to-date run-rate with a trailing/historical estimate.
- **COGS is time-varying** (effective-dated `cogs_config`); `actual → trailing_avg → plan_input →
  estimated`. An engineered **+22% COGS anomaly** (Online_Partner ERCOT North, eff mid-May vs flat plan)
  gives the COGS-spike/margin-compression alerts signal.
- **Projection:** volume = linear + trailing-21-day **cumulative** regression; weighted falls back to
  linear when thin. `is_projectable = (period == snapshot month)` (calendar fact, not GL state).
- **Fallout is proactive** via the **resolved sub-cohort** (sales older than the conv-lag SLA → final
  outcome), banded vs the channel's **own trailing baseline**; thin data → not banded (`no_data`).
- **CPA-vs-LTV:** compression on **T3M** (responsive), inversion on **T12M** (slow-burn).
- **Restatement is derived statelessly** (no state store): `frozen_reference` = CPA on spend posted
  on/before close; `restatement_delta = late_invoice_amount / conversions`.
- **Findings rank** by **normalized exceedance** (magnitude ÷ the alert's own threshold), comparable
  across alert types. **Flag-don't-suppress:** uncertain signals are flagged at magnitude with a
  confidence label, not dropped.

## Demo arc (the proof it works) — snapshots in `data/snapshots/`
`run_pipeline` across `2024-05-01/08/15/22` (pre-close) + `2024-06-08` (post-close):
- **CPA spike** HIGH: Door_to_Door North +22.8%, Telemarketing West +28.6% — `estimated=True`
  (gl_partial) at May-22, **flips to `estimated=False` (real) at June-8** (the honesty beat).
- **COGS/margin**: Online_Partner −25% margin compression HIGH from mid-May.
- **Fallout**: Telemarketing proactively +50–106% at May-22 (resolved sub-cohort) → HIGH confirmed June-8.
- **Restatement**: late-April accrued invoice → +7.8% CPA-impact update.
- Thresholds tuned in `config/system_config.yaml` (`thresholds` block + `cpa_ltv_warning_threshold`).

## Deferred (see `open_questions.md`) — none blocking BS4
First-run launch-month plan pro-rating (mid-month launch overstates volume_miss); true *post-close*
restatement needs a generator tweak (current true-up posts pre-close → reads `accrued`); confidence-aware
**display** de-emphasis (the "calm May-1" beat lives in the UI per §6 never-suppress); positional
`finding_id`.

## What's next — Build Sequence 4: narrative generation
`app/llm/narrative_generator.py` — the first LLM module. It takes the §14 findings (+ retrieved context,
BS7) and produces **prose with named placeholder tokens** (e.g. `{variance_pct}`); Python substitutes
from the finding. Orphan tokens / stray numerals fail loudly (BS5 `narrative_validator.py`). LLM endpoint
& model are **environment-configured** (`LLM_ENDPOINT/_API_KEY/_MODEL`) — never hardcoded (§16). Open
questions to resolve empirically in BS4: causal-claim strength, LLM call count/cost/latency
(batch all findings in one structured call?).

## Practical notes
- Python **3.11**, project `.venv`; pandas/numpy/pyyaml. Tests: `python3 -m pytest -q` (122 pass).
- Each module has a `python -m app.analytics.<module>` CLI that prints its output for eyeballing.
- Snapshots are committed (reproducible from the seed via `scripts/generate_snapshots.py`).
- Git: feature work on per-module branches → merged into the integration branch → `main`. All BS3
  module branches are on the remote.
