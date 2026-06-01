# Athena — Decisions Log

> **Purpose:** The *why* behind every significant choice. `athena_context.md` says what the system does; this file says why it does it that way, and what was rejected. Append-only — add entries, don't delete them. When you revisit a decision, add a new dated entry rather than editing the old one, so the evolution of your thinking stays visible.
>
> **Why this file exists separately:** It's the file where the *learning* lives. The context doc is on-rails for building; this is where you reason about tradeoffs. Read it when you want to understand a decision; the context doc when you want to implement one.
>
> **Format:** Date · Decision · What we chose · What we rejected · Why.

---

## 2024 — Foundation decisions

### Numbers are Python's, reasoning is the LLM's
**Chose:** Python calculates every number; the LLM interprets, hypothesizes, recommends.
**Rejected:** Letting the LLM do any arithmetic, even "simple" variance math.
**Why:** LLMs are unreliable at arithmetic and businesses can't trust them because of it. But the *reasoning* — pattern-matching across messy signals, hypothesizing cause, prioritizing — is exactly what LLMs are good at and is the whole reason to use one. Splitting the two keeps the trustworthy part trustworthy without neutering the valuable part. This is the spine of the entire product.

### Hallucination guard: prevent, don't audit
**Chose:** The LLM emits prose with named placeholder tokens (`{variance_pct}`); Python substitutes numbers from the structured finding. Orphan tokens and stray numerals fail loudly and deterministically.
**Rejected:** "Extract every number from the LLM's prose and re-verify it against Python values within a tolerance."
**Why:** Extract-and-verify is close to impossible to do well — the model writes "about 20%," "roughly $148," "a fifth higher," and fuzzy numeric matching produces constant false positives, which would make Athena cry wolf (violating a core principle). Constraining at generation makes the failure mode structurally impossible instead of caught after the fact. It's also a far stronger pitch line: "we make arithmetic impossible for the AI" beats "we double-check the AI's math."
**Note:** This is the headline defensibility claim. Get the module right (Phase 5). Detailed design still pending.

### Causal claims are hypotheses, not facts
**Chose:** The LLM reasons freely about likely causes and presents them naturally (an analyst's read), with supporting data always shown. It does NOT have to hedge every sentence.
**Rejected (both extremes):** (a) Tight — LLM only narrates what Python found, no causal reasoning. (b) Loose — LLM asserts causation as established fact.
**Why:** (a) is so constrained a template could do it — wastes the LLM. (b) introduces *causal* hallucination (numeric hallucination's subtler cousin): confidently asserting "X caused Y" from mere correlation. The middle — reasoning presented as a sharp sidekick's hypothesis, with data shown so the human judges the leap — keeps the value and stays honest. This is the design that makes Athena a *useful* sidekick rather than a hedge-everything liability.
**Status:** This balance is a live design question, not fully settled — see `open_questions.md`.

### Blended context doc, not separate human/AI versions
**Chose:** One `athena_context.md`, each section leading with plain-language intent then precise detail.
**Rejected:** Two docs — one for humans, one for AI tools.
**Why:** Two docs drift out of sync within a week and you lose track of which is authoritative. The things AI tools need (clear contracts, unambiguous decisions, structured schemas) are the same things that make a doc readable for humans. Structure solves it, not duplication.

### Docs split by rate of change + audience
**Chose:** Five files — context (stable architecture), decisions log (append-only reasoning), open questions (volatile), business (pitch, different audience + cadence), data dictionary (its own growing artifact).
**Rejected:** One 900-line monolith.
**Why:** Things that change at different speeds or serve different readers pollute each other in one file. A decisions log rots inside a context doc because nobody updates line 915 mid-build. Pitch language changes on a totally different cadence than architecture. Splitting by rate-of-change keeps each file trustworthy.

### `[DECIDED]` softened to `[LOCKED]` on load-bearing items only
**Chose:** Mark only genuinely load-bearing architectural commitments; pair each with a pointer here for the why.
**Rejected:** Tagging every paragraph `[DECIDED]`.
**Why:** A wall of [DECIDED] reads as defensive and, worse, half of them are "current direction" not true locks — which trains you to ignore the tag. Reserving it for real commitments keeps it meaningful and keeps Claude Code on-rails where it matters.

### Model string never hardcoded
**Chose:** `LLM_MODEL` always set via environment; the doc names no canonical model string.
**Rejected:** Defaulting to a specific dated model string in code and doc.
**Why:** Model names change. A hardcoded one becomes silently wrong and erodes trust in the doc. The whole point of the configurable endpoint is that provider/model is environment config.

---

## 2026 — Build Sequence 1 (data & config foundation)

### Generators split into a package under a single source of truth
**Chose:** Per-table generators (`gen_sales/gl/reference/notes`) orchestrated by
`generate_snapshots.py`, all drawing dimensions/dates/randomness from one
`shared.py` (seed, `SERIES` roster, calendar, noise helpers).
**Rejected:** One monolithic generator script; or independent generators each
defining their own entity/segment lists.
**Why:** Each table's logic stays understandable in isolation, and clean joins
become structural rather than hoped-for — no generator can invent a dimension
because every row's key comes from `shared.SERIES`. The orchestrator validates
the invariant (and the authored `gl_mapping.csv` against the roster) before any
write, so drift fails loudly.

### Snapshots committed to git
**Chose:** Commit `data/snapshots/` (gitignore `data/live`, `data/processed`).
**Rejected:** Gitignore snapshots and regenerate on demand.
**Why:** Output is reproducible from the seed, so committing costs little and the
demo works on a fresh clone with no generation step. Live/processed data are
runtime artifacts and stay out of git.

### Series roster spans every data-depth tier on purpose
**Chose:** Eight series deliberately covering full / short / no history, missing
revenue, and plan-only — plus a configurable `HISTORY_MONTHS` selector.
**Rejected:** A uniform set of well-behaved series that only demos the happy path.
**Why:** The product's spine is graceful degradation (§9–10): real → trailing-avg
→ plan_input. The synthetic data must *trigger* every fallback branch, or the
analytics phase has nothing to prove the fallbacks against.

### CPA spike via cumulative-target back-solving, not a hardcoded curve
**Chose:** Back-solve the hero's daily spend so cumulative-to-date CPA hits the
arc anchors exactly (before noise); coordinate spend (gen_gl) with flat
conversions (gen_sales) through `NarrativeConfig`.
**Rejected:** Hand-tuning daily spend numbers, or putting the spike in one table.
**Why:** Each cumulative snapshot then lands on its intended arc point by
construction, and the spike reads as a real CPA dynamic (cost outrunning
conversions) rather than an edited number. Retuning is one config knob.

### Output format configurable (CSV and XLSX)
**Chose:** `--format {csv,xlsx,both}`; per-table files in either format.
**Rejected:** CSV-only.
**Why:** Operational source data arrives as both in practice; supporting XLSX now
keeps the loader honest later. CSV remains the committed form for diffable demos.

### Python 3.11 as the project interpreter
**Chose:** Python 3.11 via a project `.venv`.
**Rejected:** The system Python 3.9.
**Why:** Explicit user direction; also a current, supported baseline for the
pandas/numpy stack.

---

## 2026 — Build Sequence 1 (revision: relational two-feed model)

### Operational spine split into two record-level feeds (sales + conversions)
**Chose:** Replace the single aggregate `actuals.csv` with two record-level feeds joined on a
surrogate `customer_key`: `sales.csv` (one row per submission, with `sale_date` and an
`outcome` of gained/fell_out) and `conversions.csv` (one row per gained submission, adding
`conversion_date` and `price_per_unit`). Fallout = fell_out ÷ submissions (self-contained in
`sales`); CPA = GL acquisition spend ÷ conversion count; sale→conversion lag is drawn per gain.
**Rejected:** Keeping a single aggregate daily table; a (sale_date × conversion_date) cohort
matrix; a gains-only feed with no submissions.
**Why:** A sale and a gain happen on different dates in disconnected source systems — the
single-date aggregate couldn't represent that, and a gains-only feed has no denominator for
fallout (the failures aren't in it by definition). Two record-level feeds linked by
`customer_key` mirror how an enterprise data mart actually delivers this (a sales fact and a
gains fact), keep fallout derivable, and make reporting lag fall out naturally (a gain lands
in the snapshot by its `conversion_date`). Cost: record-level grain makes the committed
snapshots large (~85 MB across the four dates) — accepted as the price of realism.

### Dimensions split into a denormalized hierarchy
**Chose:** Split the fused `entity` ("ERCOT North") into `entity` (market) → `region` →
`service_territory`, and add a nested customer split (`customer_size_tier` →
`customer_class`, class residential-only) plus `contract_term_months` (Term only). Column
names stay generic; values stay domain-specific. The roster stays a curated ~10 series
spanning every dimension value and fallback tier — never the full cross-product.
**Rejected:** Keeping the fused entity; making customer_class/size_tier independent (would
force nonsense combos like large_C&I + single_family); enumerating the Cartesian product.
**Why:** Data marts arrive pre-dimensioned this way, and a curated roster keeps the demo arc
legible while still exercising each dimension and each metric fallback.

### `revenue_per_unit` → `price_per_unit`
**Chose:** Rename and relocate to `conversions.csv`, defined as the contracted price/rate
known at signing (nullable; null → plan-margin fallback). Margin stays `price_per_unit −
cogs_per_unit`.
**Rejected:** Keeping `revenue_per_unit` (ambiguous — read as realized revenue, which isn't
known at gain time); dropping price entirely and sourcing it only from plan.
**Why:** The field only ever fed margin, and "price" is what's actually known when a customer
converts; realized revenue accrues later and belongs to a future billing feed. ⚠️ This touches
**LOCKED** text — `athena_context.md` §10 (margin formula) and §14 (structured finding) — so
those field names were reconciled alongside this entry.

**Follow-up (analytics phase, not blocking generation):** `config/cogs_config.csv` and
`config/retention_config.csv` are still keyed by `entity, segment` and will need a `region`
column to stay aligned with the wider series identity. `generate_snapshots.py` doesn't read
them, so generation is unaffected until those tables are consumed.

---

## 2026 — Build Sequence 1 (revision 2: no-lookahead fallout)

### Sales feed carries no outcome — fallout is derived by anti-join
**Chose:** Drop the `outcome` column from `sales.csv`. A submission records only that it
happened (`customer_key`, `sale_date`, dims). `gen_conversions` decides which submissions
convert and emits gains sharing `customer_key`; **fallout = submissions with no matching
conversion**. The gain decision (and the fallout ramp) moved from `gen_sales` into
`gen_conversions`.
**Rejected:** Stamping `gained`/`fell_out` on the sale at `sale_date` (revision 1); a separate
module to "combine" sales and gains.
**Why:** Stamping the outcome at sale time let a snapshot reveal the result of an enrollment
that, on that date, is still pending — a look-ahead that contradicted the whole point of the
conversion lag. Deriving fallout by `customer_key` anti-join means a not-yet-converted sale
simply has no gain yet (honest: you can't know an unresolved outcome). It also matches how the
two systems actually sit in a data mart — a sales fact and a gains fact joined by a key — and
keeps the join (not a bespoke module) as the mechanism. Consequence: fallout is only *resolved*
for submissions older than the max conversion lag, so it is a lagging/projected signal, not an
instantly-confirmed one. This supersedes revision 1's "one row per outcome."

### Conversion lag shortened to fit the demo window
**Chose:** `conv_lag` mean 3 / max 7 days (was 7 / 21).
**Rejected:** Keeping the longer lag.
**Why:** With no-lookahead, an outcome can't be observed until its gain lands. A 21-day max lag
left nearly every May sale unresolved across the four May snapshots, flattening the hero CPA
climb and hiding the fallout ramp. A ~week lag is still realistic for enrollment→flow and lets
both signals develop within the demo window.

### customer_key uses per-series integer blocks
**Chose:** `customer_key = (series_index+1) × 1,000,000 + local_index`.
**Rejected:** A single global 1..N counter across all series.
**Why:** The global counter coupled every series — retuning one series' volume renumbered all
later keys, producing huge spurious diffs in the committed snapshots. Per-series blocks keep
keys as plain integers while isolating each series, so a volume change only churns that series.

### Period finality: align the close grace to the gain SLA (no clamp), + a post-close snapshot
**Chose:** Set `period_close_day` 5 → 8 so the close grace (8 days after month-end) is ≥ the
conversion-lag SLA (max 7 days). By close, every in-period sale's gain has landed, so the
closed period is **final and complete by construction** — no clamping of conversion dates.
Added a fifth, post-close snapshot (2024-06-08) showing the settled month.
**Rejected:** Clamping every gain to land by month-end (special-cases the data, compresses the
lag for last-week sales); keeping close < SLA and treating the routine trailing gains as
restatements.
**Why:** Real books close a few days *after* month-end precisely to capture trailing activity;
matching the close grace to the gain SLA captures it cleanly without a clamp hack and keeps the
data realistic. A gain that lands *beyond* the SLA (after close) is then a genuine anomaly —
handled as a restatement flag via the existing GL `restated`/`accrued` states and the "Period
restatement" alert (Phase-3 risk classifier), which is the right home for "if it materially
changes fallout, flag it." The post-close snapshot lets the demo contrast the pre-close
projection (May 22) against the final settled actuals (June 8) — notably fallout, a lagging
signal that only fully resolves once gains land.

### Weekday-only channels: zero weekend activity; dead config removed
**Chose:** `weekday_factor` returns 0.0 (was 0.15) on weekends for field/broker segments;
removed the unused `cpa_spike_start_day` knob and the now-unused `OUTCOME_*` constants.
**Why:** "Weekday-only" should mean no weekend submissions (cleaner, and helps the Phase-3
`business_days` plan pro-rating); unused config is misleading and rots.

---

## Template for new entries

```
### <short title>
**Chose:**
**Rejected:**
**Why:**
**Status (if not final):**
```
