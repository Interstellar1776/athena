# Athena — Build Log

> **Purpose:** A dated, append-only record of *what was built* in each Build
> Sequence phase (`athena_context.md` §19) — the concrete artifacts, the
> verification that gates the phase, and any deviations worth a reader's
> attention. Complements `decisions_log.md` (which records *why* choices were
> made) and `data_dictionary.md` (which defines the data contract).
>
> **Format:** one section per phase, newest at the bottom. Each section states
> what shipped, how it was verified, and what's deliberately deferred.

---

## Phase 1 — Data & config foundation  ·  2026-05-31  ·  COMPLETE

Built the synthetic snapshot data and static config/reference tables that every
later phase computes on. No analytics, LLM, or `app/` code — foundation only.

### Artifacts

**Generators (`scripts/generators/`)** — single-responsibility, all drawing
dimensions/dates/randomness from `shared.py`:
- `shared.py` — single source of truth: master seed + per-stream RNG factory,
  calendar (configurable `HISTORY_MONTHS` selector, active period, snapshot
  dates), dimension vocabularies, the GL chart-of-accounts fragment, the
  `SERIES` roster, derived helpers (incl. canonical GL mapping), noise/shaping
  helpers, and the `NarrativeConfig` tuning dataclass.
- `gen_sales.py` → `actuals.csv` — daily volume in/converted/lost + revenue.
- `gen_gl.py` → `gl_actuals.csv` — daily acquisition + monthly overhead spend,
  plus the engineered late/accrued entry.
- `gen_reference.py` → `reference_data.csv` — monthly plan rows for every
  series; forecast rows for flagged series.
- `gen_notes.py` → `operational_notes.csv` — authored qualitative notes.

**Orchestrator** — `scripts/generate_snapshots.py`: establishes config + seed,
runs the generators, **validates join integrity and halts loudly before any
write**, assembles the four cumulative snapshots, prints an inspection summary.
CLI: `--format {csv,xlsx,both}`, `--history-months`, `--cpa-spike-magnitude`,
`--fallout-target-rate`, `--forecast-divergence`, `--noise-sd`, `--no-late-invoice`.

**Static config (`config/`)** — `gl_mapping.csv`, `retention_config.csv`,
`cogs_config.csv`, `system_config.yaml`. `gl_mapping.csv` is validated against
`shared.canonical_gl_mapping_rows()` so it cannot silently drift from the roster.

**Generated data** — `data/snapshots/{2024-05-01,-08,-15,-22}/` (CSV, committed).
Project scaffolding: `requirements.txt`, `.gitignore` (commits snapshots; ignores
`data/live`, `data/processed`, `__pycache__`).

### The series roster (the join backbone, `shared.SERIES`)

Eight `(entity, segment, product_type)` series engineered to span every
data-depth tier so downstream fallback logic all has data that triggers it:

| entity / segment / product_type | role | exercises |
|---|---|---|
| ERCOT North / Paid Search / Term | hero | CPA-spike HIGH; CPA-vs-LTV compression; has forecast |
| ERCOT North / Door_to_Door / Month_to_Month | stable | control (proves no crying wolf) |
| ERCOT Coast / Paid Search / Term | stable | has forecast (plan-vs-forecast gap) |
| ERCOT Coast / Door_to_Door / Month_to_Month | fallout | fallout-rate MEDIUM |
| PJM East / Broker / Term | stable | plan-only; prior_year_same_period COGS |
| PJM East / Affiliate / Month_to_Month | missing_revenue | margin plan-input fallback |
| ERCOT Coast / Affiliate / Term | short_history | < 3-month trailing fallback (from 2024-03-01) |
| ERCOT West / Broker / Term | new | first-run plan_input fallback (launches 2024-05-15) |

### The demo arc (verified output)

| Snapshot | Hero May CPA vs plan | Fallout MTD | Signals appearing |
|---|---|---|---|
| 2024-05-01 | −0.1% | 14.3% | calm — nothing crosses |
| 2024-05-08 | +6.3% | 16.5% | hero campaign note visible (drift) |
| 2024-05-15 | +13.4% | 19.0% | forecast rows + ERCOT West actuals appear (building) |
| 2024-05-22 | +21.6% | 21.9% | late/accrued invoice posts; clears +20% HIGH (confirmed) |

The hero CPA spike is the *product* of two coordinated generators — `gen_gl`
ramps spend on a cumulative-target schedule while `gen_sales` holds conversions
flat — never hardcoded into one table. Multiplicative noise (~4%) layers on top
without burying the signal.

### Verification (the realism gate)

- **Joins & invariants** — actuals→plan and GL→gl_mapping merges resolve in
  every snapshot; `volume_converted ≤ volume_in`, no negatives. ALL PASS.
- **Loud-halt** — negative tests confirm invented dimensions, gl_mapping drift,
  and invariant breaks each halt the pipeline before writing.
- **Fixtures** — late/accrued entry only in May-22; hero note from May-8;
  forecast from May-15; ERCOT West plan-only until launch; short-history series
  from Mar-1; missing-revenue series all-null.
- **Reproducible** — byte-identical re-run (seed determinism).
- **Format** — CSV and XLSX both write and read back.

### Decisions & deviations (see `decisions_log.md` for rationale)
- Snapshots are **committed** to git (reproducible; demo works on fresh clone).
- Output is **format-configurable** (CSV and/or XLSX); CSV is the committed form.
- **Python 3.11** is the project interpreter (`.venv`).
- Generated table schemas conform exactly to `data_dictionary.md`; a few
  generator *conventions* (forecast issue-date dating, snapshot cut by
  `posting_date`, the `ALL` notes sentinel) are now recorded there.

### Known gap carried forward
Strict trailing-12-month CPA-vs-LTV compression (§11) does not fire from a
single-month spike — the hero T12M/LTV ratio sits at 0.764 (a rising *watch*),
while in-month economics are clearly compressed. Logged in `open_questions.md`
for Phase 3 to resolve (trailing-3-month basis vs. revised §11 wording).

### How to run
```bash
python scripts/generate_snapshots.py                 # default, CSV
python scripts/generate_snapshots.py --format both   # CSV + XLSX
python scripts/generate_snapshots.py --cpa-spike-magnitude 0.30 --no-late-invoice
```

### Deferred to later phases
No `app/` modules, ingestion validator, analytics, or LLM. `data/processed/`
and `data/contextual/` are created when the phase that needs them arrives.

> **Note (historical):** the roster table above reflects the *original* taxonomy
> (Paid Search / Broker / Affiliate, single `actuals.csv`). It was later revised to
> acquisition-method channels (Web_Direct, Door_to_Door, Telemarketing,
> Inbound_Call_Center, Direct_Mail, Online_Partner), a two-feed `sales`+`conversions`
> model, a dimension-free GL ledger, and a roster fan-out — see `decisions_log.md`
> (BS1 revisions 3–7). This section is kept as the dated original record.

---

## Phase 2 — Ingestion validation  ·  COMPLETE

`app/validation/ingestion_validator.py` — the gate that halts loudly on bad data
before any analytics. Schema/type/range/join-integrity checks; `PipelineHalt`
(a `ValueError`) with file+field messages; "facts must have a reference" and
"every fact tuple exists in the roster" enforced. `build_contracts()` builds the
validation contract from the config tables. Negative-test fixtures
(`tests/fixtures/`) confirm halt-on-bad-data; clean snapshots pass through.

---

## Phase 3 — Analytics core  ·  2026-06-04  ·  COMPLETE

The full deterministic analytics pipeline: validated snapshot → ranked §14 findings,
runnable end-to-end from one entry (`variance_engine.run_pipeline`). **122 tests pass.**

### Modules (`app/analytics/`, built in the §15 order)
- `data_loader.py` — single I/O owner: read → **ingestion gate** → type/normalize; one
  dict of frames, identical in snapshot/live mode (data_cleaner folded in).
- `data_merger.py` — aggregate-then-join facts↔reference↔GL to leaf×period (no row
  explosion); resolves the dimension-free GL to unit grain.
- `gl_processor.py` — GL completeness state (open/closed/restated/accrued) + late-invoice
  flags, keyed to the document month; close = following-month day-8.
- `metrics_calculator.py` — CPA (monthly/T3M/T12M, unit grain), COGS (effective-dated,
  time-varying), margin, LTV, fallout — each labeled with its method; `is_projectable`.
- `projection_engine.py` — period-end **volume** projection (linear + trailing-21-day
  cumulative regression) and **proactive fallout** (resolved-sub-cohort, lag-corrected);
  current period only.
- `risk_classifier.py` — scores **every** metric × period × grain HIGH/MEDIUM/LOW/INFO over
  a 6-month window; magnitude-only severity + orthogonal `estimated`; the §11 alert stack;
  thresholds in `system_config.yaml`.
- `findings_builder.py` — rolls non-LOW assessments into §14 findings (unit-aggregate headline,
  leaf drill-down, one volume finding with both projections); ranks by normalized exceedance.
- `variance_engine.py` — orchestrator: threads the pure cores **once**; rich result
  (findings + assessments + intermediates + summary); fail-loud per stage.

### Demo data addition
- Engineered **COGS anomaly** on Online_Partner ERCOT North (+22% effective mid-May vs a flat
  plan) so the COGS-spike / margin-compression alerts have signal (only `config/cogs_config.csv`
  changed; snapshots untouched).

### Verified demo arc (via `variance_engine.run_pipeline`)
- **CPA spike** HIGH: Door_to_Door North +22.8%, Telemarketing West +28.6% (estimated/gl_partial
  at May-22 → real at June-8 — the `estimated` flag flips True→False across close).
- **COGS/margin**: Online_Partner −25% margin compression HIGH from mid-May.
- **Fallout**: Telemarketing proactively +50–106% at May-22 (resolved sub-cohort) → HIGH confirmed
  at June-8.
- **Restatement**: late-April accrued invoice surfaces as an update (+7.8% CPA impact).
- Calibration prevents crying wolf: pending/`no_data` fallout not banded, first-run volume flagged
  low-confidence, exceedance-normalized ranking.

### Deferred (see `open_questions.md`)
First-run launch-month plan pro-rating; true post-close restatement data; confidence-aware display
de-emphasis (the "calm May-1" beat is a display-layer concern, per §6); positional `finding_id`.
