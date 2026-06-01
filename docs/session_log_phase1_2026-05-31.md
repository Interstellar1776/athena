# Session Log — Phase 1 Build (2026-05-31)

> **What this is:** A chronological, plain-language narrative of the working
> session that produced Athena's data & config foundation — including the
> decisions made interactively, the requests that came mid-stream, the tuning
> iterations, and how everything was verified. Written so the session can be
> re-read without scrolling a terminal.
>
> For the *formal* phase record see [`build_log.md`](build_log.md); for the
> *why* behind each choice see [`decisions_log.md`](decisions_log.md).

---

## 1. Orientation

The session opened by reading the project's source-of-truth docs end to end:
`CLAUDE.md`, then `athena_context.md`, `data_dictionary.md`, `decisions_log.md`,
`open_questions.md`, and `business.md`. Confirmed understanding and restated the
three architectural rules being held to: **Python owns every number / the LLM
emits no numerals**, **build the full pipeline thin before deepening any layer**,
and **fail loud + label everything**.

The repo at this point held only `docs/` and `CLAUDE.md` — a greenfield start.

## 2. Planning Build Sequence 1 (data & config foundation)

Entered plan mode. The requested shape: per-table generators under
`scripts/generators/` (one responsibility each) fed by a single `shared.py`
source of truth, orchestrated by `scripts/generate_snapshots.py`, with static
config tables authored by hand.

Three clarifying questions were asked before finalizing the plan, and answered:

1. **Commit snapshots to git?** → Yes, commit them (reproducible from the seed;
   demo works on a fresh clone).
2. **How much to scaffold now?** → Phase-1 folders only (no premature `app/`).
3. **History depth?** → 12+ months, but make it a **configurable selector**, and
   explicitly include short / missing / no-history series so the plan/forecast
   fallbacks are demonstrable. (This steer shaped the whole series roster.)

The plan — including a build/data-flow DAG — was written to the plan file and
approved.

## 3. Two requests that arrived mid-build

- **"Use Python 3.11."** The system only had 3.9 (no pandas). Installed Python
  3.11 via Homebrew, rebuilt the project `.venv` against it
  (3.11.15 · pandas 3.0.3 · numpy 2.4.6 · pyyaml · openpyxl).
- **"Should be able to work on a .csv and an .xlsx."** Made the snapshot writer
  format-configurable (`--format {csv,xlsx,both}`) and added `openpyxl`.

## 4. What was built

- **`scripts/generators/shared.py`** — the apex of the DAG: master seed +
  per-stream RNG factory, calendar (configurable `HISTORY_MONTHS`, active period,
  snapshot dates), dimension vocabularies, GL chart-of-accounts fragment, the
  eight-series `SERIES` roster with baseline economics, derived helpers
  (including the canonical GL mapping), noise/shaping helpers, and the
  `NarrativeConfig` tuning dataclass.
- **`gen_sales.py / gen_gl.py / gen_reference.py / gen_notes.py`** — the four
  single-responsibility generators, each iterating `shared.SERIES` only.
- **`scripts/generate_snapshots.py`** — orchestrator: config + seed → generate →
  **validate joins/invariants and halt loudly before writing** → assemble the
  four cumulative snapshots → print an inspection summary. CLI knobs for every
  narrative lever.
- **`config/`** — `gl_mapping.csv` (validated against the roster),
  `retention_config.csv`, `cogs_config.csv`, `system_config.yaml`.
- **Project files** — `requirements.txt`, `.gitignore` (commit snapshots; ignore
  `data/live`, `data/processed`, `__pycache__`, `.venv`).

The eight series were chosen to span every data-depth tier — full / short / no
history, missing revenue, plan-only — so downstream fallback logic all has data
that triggers it. The hero (ERCOT North / Paid Search) carries the CPA spike;
ERCOT Coast / Door_to_Door carries the fallout; ERCOT West is the brand-new
first-run series.

## 5. Tuning the arc (the interesting part)

First run produced a monotonic arc but it landed **low** — the May-22 hero CPA
came in at +18.7%, just under the +20% HIGH line I wanted it to clear, and the
fallout MTD rate was barely moving (~14–15%).

Diagnosed both:
- **Hero spike low** → ran the generator with `--noise-sd 0`. Noise-free hit the
  targets almost exactly (+1.4 / +8.5 / +15.5 / +22.5%), proving the
  cumulative-target back-solve was correct and the shortfall was just this seed's
  noise. Fix: gave the spike headroom (default magnitude 0.22 → **0.25**) and
  re-expressed the arc anchors as fractions of the magnitude.
- **Fallout flat** → the cumulative month-to-date rate was being diluted by the
  calm early-period days. Fix: start the ramp on day 1 and let it reach a higher
  daily target (**0.30**) by the confirmed day-22 snapshot, so MTD visibly
  climbs.

After the fix the arc reads cleanly:

| Snapshot | Hero CPA vs plan | Fallout MTD | |
|---|---|---|---|
| 05-01 | −0.1% | 14.3% | calm |
| 05-08 | +6.3% | 16.5% | drift (hero note appears) |
| 05-15 | +13.4% | 19.0% | building (forecast + ERCOT West appear) |
| 05-22 | +21.6% | 21.9% | confirmed HIGH (late invoice posts) |

## 6. Verification

- **Joins & invariants** — actuals→plan and GL→gl_mapping merges resolve in every
  snapshot; `volume_converted ≤ volume_in`; no negatives. ALL PASS.
- **Loud-halt** — negative tests confirmed invented dimensions, gl_mapping drift,
  and invariant breaks each halt the pipeline before any write.
- **Fixtures** — late/accrued entry only in May-22; hero note from May-8;
  forecast from May-15; ERCOT West plan-only until its May-15 launch;
  short-history series from Mar-1; missing-revenue series all-null.
- **Reproducible** — byte-identical re-run.
- **Formats** — CSV and XLSX both write and read back.

## 7. One honest gap, surfaced not hidden

Strict **trailing-12-month** CPA-vs-LTV compression (§11) won't fire from a
single-month spike — a year-long average barely moves. The hero T12M/LTV ratio
sits at 0.764 (a rising *watch*), while in-month economics are clearly compressed
(May CPA ≈ 0.96 of LTV). Rather than bend a `[LOCKED]` alert definition silently,
this was logged in `open_questions.md` for Phase 3 to resolve (trailing-3-month
basis vs. revised §11 wording). The data supports either direction.

## 8. Documentation updated

- **New:** `docs/build_log.md` (formal phase record) and this session log.
- **`decisions_log.md`** — a `2026 — Build Sequence 1` section with the rationale
  for six choices.
- **`open_questions.md`** — the T12M compression question (Phase 3).
- **`data_dictionary.md`** — table field schemas already matched exactly; added
  generator *conventions* (forecast issue-date dating, `"ALL"` notes sentinel,
  snapshot cut rules) and checked off the Phase-1 generation checklist.

## 9. Where things stand / next

Phase 1 is complete and verified. Next in the locked build sequence is
**Phase 2 — `ingestion_validator.py`**, which builds directly on the
halt-on-bad-data validation already proven in the orchestrator.

### How to run
```bash
python scripts/generate_snapshots.py                 # default, CSV
python scripts/generate_snapshots.py --format both    # CSV + XLSX
python scripts/generate_snapshots.py --history-months 6 --cpa-spike-magnitude 0.30
```
