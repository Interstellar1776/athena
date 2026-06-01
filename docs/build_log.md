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
