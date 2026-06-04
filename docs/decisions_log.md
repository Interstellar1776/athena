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

## 2026 — Build Sequence 1 (revision 3: channel taxonomy + raw GL ledger)

### Channel (`segment`) taxonomy revised to acquisition methods
**Chose:** Replace the four generic segments (Paid Search / Door_to_Door / Broker / Affiliate)
with six acquisition channels: Web_Direct, Door_to_Door, Telemarketing, Inbound_Call_Center,
Direct_Mail, Online_Partner. Re-cast the roster to 12 curated series across them (every demo
role + data-depth tier preserved). The hero moved Paid Search → Web_Direct; fallout stays
Door_to_Door.
**Rejected:** Keeping the four generic segments; or rolling finer GL methods up to them via the
mapping (keeps a naming mismatch between the ledger and the gain channels).
**Why:** The ledger's cost_center *is* the channel, so the gain channels and the GL channels
must be the same vocabulary. A realistic acquisition-method taxonomy makes the ledger and the
mapping read true. Cost: the sales/conversions/reference feeds re-generate (segments live on
every feed), so this reaches back into the merged sales/gains work — accepted.

### gl_actuals is a dimension-free raw ledger; meaning lives in a vendor-keyed mapping
**Chose:** `gl_actuals.csv` carries only cost_center (= channel, numeric + description),
gl_account (= expense type, numeric + description), amount, vendor, dates, description — no
entity/region/segment. `gl_mapping.csv` keyed on **(cost_center, gl_account, vendor)** resolves
each entry to (segment, entity, region, spend_category). CPA is computed at the channel ×
geography grain. Kept a token overhead account for the spend_category split.
**Rejected:** Carrying business dimensions on the ledger (today's shape); a 1:1
cost_center→series map without vendor.
**Why:** That's the real ERP→BI boundary — a GL is just cost centers, accounts, vendors and
amounts; a mapping/allocation layer assigns business meaning. Vendor in the key is load-bearing:
the same (cost_center, gl_account) resolves to different regions/channels by vendor (e.g.
Door-to-Door 5020/6030 → ERCOT South via FieldForce, ERCOT North via DoorPoint).

### Spend emitted as periodic invoices with per-channel cadence
**Chose:** Replace daily aggregate rows with periodic per-vendor invoices billed in arrears —
**weekly** for Web_Direct/Door_to_Door/Telemarketing/Inbound, **monthly** (month-end) for
Direct_Mail/Online_Partner. A daily noise-free target (hero arc preserved) is aggregated into
invoices; bonus channels book a monthly slice to account 6040; multi-vendor units split the
invoice. Added a post-close **restatement** (May document, June-6 posting) alongside the
existing late April invoice.
**Rejected:** One aggregate row per day per channel.
**Why:** Reads like a real ledger and the cadence *is* the demo: weekly channels show the spike
progressing each snapshot; monthly channels have no in-month spend early, so their CPA falls
back to an estimate until the post-close snapshot — exercising the GL completeness/estimation
states for free. Also removes the $0 weekend-row artifact.

---

## 2026 — Build Sequence 1 (revision 4: hero on a face-to-face channel)

### Hero moved to Door_to_Door (field sales); spike driven by commissions + incentives
**Chose:** The CPA-spike hero is **Door_to_Door** (face-to-face field sales), not Web_Direct.
Web_Direct stays as a stable "online advertising" channel; fallout moved to Telemarketing
(agent turnover). The hero's spike rides its **weekly commission** ramp (visible across the
pre-close snapshots) plus an **additive** month-end **incentive bonus** (account 6040) that
lands only in the post-close snapshot. The conversion lag was tightened (mean 2 / max 4 days)
so the weekday-only field channel's gains land within the snapshot cadence and the CPA climb
stays visible.
**Rejected:** Keeping the hero on Web_Direct; carving the bonus out of commissions (would
flatten the visible pre-close spike); leaving the longer 3/7-day lag (left the climb masked by
the lag's early conversion-undercount for the bursty weekday-only channel).
**Why:** Digital/programmatic spend is flat and elastic (you set a budget, the platform spends
it) — CPA there doesn't realistically balloon. CPA *does* run away on a labor-intensive
face-to-face channel: chasing a Q2 target piles on commissions, short-term spiffs/incentives,
and field hours while conversions stay flat. (Base salary is fixed opex, not a variable
acquisition cost, so it's deliberately excluded.) This makes the headline spike read true, and
the month-end incentive surge gives the post-close snapshot a second, realistic jump alongside
the restatement. Supersedes revision 3's "hero → Web_Direct."

---

## 2026 — Build Sequence 1 (revision 5: roster fan-out)

### Each channel×geography unit fans into a product/customer mix
**Chose:** Keep the ~12 channel×geography **units** as the economic backbone, but derive the
record-level roster as **leaf series = units × a 2–3-combo mix** over `product_type` /
`contract_term_months` / `customer_class`. The unit's volume splits across the mix by weight;
**economics are uniform across a unit's leaves** (the finer dims are labels today). The
record-level generators (sales/conversions/reference) iterate leaves; `gen_gl` and `gl_mapping`
stay at the **unit** grain. `customer_size_tier` stays unit-level (no residential/C&I blend).
**Rejected:** One tuple per channel×region (the finer dims were frozen single-values — nothing
real to slice); fanning `customer_size_tier` too (would force uniform economics across very
different customer types); making the dims drive economics now (deferred).
**Why:** Real acquisition in a channel×region spans a mix of products/customers. Fanning out
gives the data an honest cross-section to slice by, and sets up the structure now so the demo
can show sub-segments — while the economic backbone (and the CPA/fallout story) is untouched.
Differentiated economics per term/class/tier can be layered on later.

---

## 2026 — Build Sequence 1 (revision 6: config tables derived from the roster)

### cogs_config & retention_config derived per-sub-segment + validated (drift-guard)
**Chose:** Generate `config/cogs_config.csv` and `config/retention_config.csv` from the roster
(`canonical_cogs_config_rows()` / `canonical_retention_config_rows()`), one row per **sub-segment
(leaf)** keyed by the full dimension hierarchy, and have the orchestrator **validate them ==
canonical** (halt on drift) — the same pattern as `gl_mapping`. Each unit can override `cogs` /
`retention` by `product_type` or by an exact sub-segment (precedence: sub-segment > product_type
> unit default); overrides resolve onto the leaves at derive time, so the config tables **and**
the plan feed (`gen_reference`'s `cogs_ref` / `ltv_ref`) read the same per-leaf values.
**Rejected:** Hand-authored config tables (how they drifted to the old taxonomy in the first
place); unit-grain rows (the user wanted per-sub-segment structure for later tuning); COGS in a
single place (kept the §10 two-place model — see below).
**Why:** These tables are Phase-3 inputs (COGS→margin, retention→LTV) and had silently gone
stale (old `ERCOT North` / `Paid Search` taxonomy) because they duplicated roster economics by
hand. Deriving + validating makes drift impossible and gives a real per-segment tuning knob
without restructuring later. Snapshots are unaffected (the pipeline doesn't read these tables).

### COGS two-place model affirmed (not a deviation)
**Chose:** Keep COGS as both a standing configured input (`cogs_config`) and a plan value
(`reference_data.cogs_ref`), the latter being the fallback. Both come from one per-leaf source.
**Why:** Matches LOCKED §10 ("COGS is a plan input… configured in a reference table"; fallback
chain ends in plan COGS) and is realistic — a standing rate sheet *and* a budgeted figure. The
one-source derivation removes the only real downside (the two drifting apart).

---

## 2026 — Build Sequence 1 (revision 7: plan/forecast grain + GL tie-back)

### Plan/forecast grain locked to actuals + GL; plan kept clean with a bias knob
**Chose:** Make `reference_data`'s grain contract explicit and enforced: **volume** targets at
sub-segment (leaf) grain (vs. record-level sales/conversions); **cost/CPA** targets a unit
(channel×geography) plan allocated across leaves, so `Σ cost_ref` per `(entity, region, segment)`
reconciles to actual GL spend (which `gl_mapping` resolves to the same key). Added a validator
that every acquisition unit `gl_mapping` resolves to has an active-period plan row (every GL
dollar has a plan CPA to compare). Plan volume/CPA stay the noise-free baseline by default; a
per-unit `plan_bias` knob (empty default) allows realistic independent plan error later.
**Rejected:** Baking plan error in now (would disturb the calibrated spike/fallout vs. calm
contrast); a separate plan/forecast file (kept one `reference_data` per the data owner).
**Why:** The plan is the comparison baseline and the bottom of every fallback chain — it must
join cleanly to both the leaf-grain actuals and the unit-grain GL. The structure already did
this (uniform `base_cpa` per unit); this revision documents + enforces it and adds the seam for
realistic plan misses without touching today's demo signal. `plan_bias` empty ⇒ snapshots
unchanged.

---

## 2026 — Build Sequence 3 (analytics core)

### data_loader is the single I/O owner: read → gate → type, one structure for both modes
**Chose:** `app/analytics/data_loader.py` reads the raw files once (as strings), runs the
ingestion validator and **halts loudly on bad data before returning**, then types/normalizes
and hands back one dict of dataframes — identical in snapshot or live mode. Config tables are
read once and reused to build the validation contract (extracted `build_contracts()` from the
validator). Dates parse with `errors="raise"` (never a silent `NaT`); dimension keys stay
**canonical strings** (`contract_term_months` = `"12"`/`""`, never `12.0`/`"nan"`).
**Rejected:** A passthrough loader with validation wired as a separate downstream step;
coercing dates silently; leaving keys as pandas-inferred floats.
**Why:** "Never analyze unvalidated data" is a standing rule, so the gate belongs at the mouth
of the pipeline. String-native dimension keys make every fact↔reference↔config join match
byte-for-byte (a float `12.0` key silently fails to join an M2M `""`), and one disk read keeps
config from being loaded twice.

### data_merger aggregates actuals to leaf×period, then joins plan 1:1 (never record-level)
**Chose:** Roll sales/conversions up to **leaf × period**, then 1:1 left-join the monthly plan;
GL is **resolved only** (geography attached at unit grain, overhead filtered out) — its period
bucketing/completeness is `gl_processor`'s job. Forecast rides in parallel `*_ref_fc` columns;
fallout is reconciled on `customer_key`. Conversions are counted on both axes (`landed` by
conversion_date for CPA/volume; `cohort` by sale month).
**Rejected:** Record-level join of facts to the monthly plan (multiplies each actual by 3–13
monthly plan rows — a row explosion); aggregating GL in the merger.
**Why:** `reference_data` is a monthly target and the feeds carry a year of trailing history;
aggregate-then-join is how a real variance pipeline avoids the explosion and hands the metrics
layer clean comparison frames at the grain it needs.

### gl_processor: completeness keyed to the document month, close = following-month day 8
**Chose:** `period = document_date` month — **spend ties to the month it belongs to**. Close
date = day `period_close_day` (8) of the *following* month (May closes June 8). State per
`(unit, period)`, first match: `open` → `accrued` → `restated` → `closed`. Late invoices
(posting month ≠ document month) are flagged in dedicated columns, attributed to the document
month, **independent of the state label** (so both engineered invoices are always detected).
**Rejected:** Period by posting date; the literal spec order `restated → accrued` (leaves
`accrued` unreachable and mislabels the late-April invoice).
**Why:** A cost belongs to the period it documents to, not when it happened to post; checking
`accrued` before `restated` makes a prior-month invoice landing in the open month read as
`accrued` (the §10 intent). Close on the following-month day-8 matches the post-close June-8
snapshot and the conversion-lag SLA.
**Status (open):** under `current_period = snapshot month`, the post-close May true-up reads
`accrued` (it posted in the current month) rather than `restated`; labeling it `restated` would
require treating a post-close snapshot as *settling the prior month*. Deferred until the output
is reviewed against the demo.

### Fallout is shown raw, as computed at each snapshot — not lag-filtered
**Chose:** Report fallout exactly as it stands in each cut: `unmatched` = submissions with no
matching gain *yet*, pending (not-yet-landed) cohorts included. No "resolved-cohort" filter.
**Rejected:** Holding back cohorts younger than the conversion lag (or the period close) from
the displayed fallout.
**Why:** The demo's value is *watching the fallout signal build* — raw unmatched climbs across
the pre-close snapshots (≈21.9k → 22.4k → 22.8k → 23.4k) and partially settles post-close as
lagged gains land; that lagging behavior **is** the story, and suppressing it defeats the point.
Consistent with §9 (never blank — label/contextualize, don't hide): the lag/confidence caveat
belongs to the narrative + risk layers, not to dropping the number. Supersedes the earlier
(un-built) intent to exclude pending cohorts from the fallout rate.

### CPA stays unit-grain; volume/margin stay leaf-grain (roll up to join)
**Chose:** Keep CPA at the `(entity, region, segment)` unit grain (where GL resolves) and
volume/fallout/margin at leaf grain; roll leaves up to the unit when a join needs it, rather
than forcing one shared grain.
**Rejected:** Coercing everything to a single grain in the merger.
**Why:** GL carries no product/customer dimensions, so unit-grain CPA is the honest grain;
forcing a leaf grain would fabricate a precision the ledger doesn't have. Findings can carry a
different grain per metric (§14).

---

## 2026 — Build Sequence 3 (analytics core — planning decisions, pre-implementation)

> These were settled in a planning conversation before writing `metrics_calculator` and the
> downstream modules. They refine several **[LOCKED]** items in `athena_context.md` (§6 §10 §11
> §14 §15); each refinement is called out so the change is deliberate, not silent.

### Period lifecycle: compute forever, label-and-freeze at close
**Chose:** A period is **always computable** — closing is a *label*, not a removal. CPA (and every
metric) recomputes whenever new spend lands, in any period, at any snapshot. Lifecycle
`open → closed → (restated)`, with `accrued` as the cross-period-posting marker. `open` = current
month before close day (the proactive, projected zone); `closed` = past close, no new spend
(authoritative; freeze a settled reference; no projection); `restated` = past close, new spend
appeared (recompute, flag delta vs. the frozen reference; no projection); `accrued` = document date
prior period, posting date current. Close day = `period_close_day` (8).
**Rejected:** Treating a closed period as frozen/uncomputable (the implicit prior model, where
"past close" meant the metric stopped moving); deriving completeness only from posting dates.
**Why:** Live mode is the real test — a **March snapshot** must naturally show Jan/Feb `closed` and
March `open` with no special-casing, and a restatement must be a *recompute against a baseline*,
not a frozen blank. "Compute forever, label the trust level" gives that for free and keeps the
never-blank principle (§9) intact: a settled period still has live numbers, just authoritative ones.

### Accrued vs. restated: keep `accrued`-before-`restated`; the June-8 May true-up is `accrued`
**Chose:** Keep the built `gl_processor` behavior — evaluate `open → accrued → restated → closed`,
`accrued` **before** `restated`. The May true-up (May document date, **posts June 6**), seen at the
**June-8 snapshot**, reads **`accrued`**.
**Rejected:** A `restated`-first ordering / labeling the true-up `restated` (briefly drafted on the
strength of "June-8 settles May," then reversed by the owner).
**Why (owner's decision + the engineering case for it):** The true-up **posts June 6, which is
before May's close (June 8 = `period_close_day` of the following month).** It therefore arrived
*within* May's settlement window — a prior-period (May) cost landing in the open current month
(June), which is the textbook definition of an **accrual**, not a restatement. A `restated` entry is
one that posts **after** a period's close (a genuinely settled month changing). Checking `accrued`
first yields the accounting-correct label here without special-casing. This resolves the earlier
deferred *Status (open)* note on the BS3 `gl_processor` entry above in favor of the existing code —
**no `gl_processor` change is required.**
**My recommendation (noted for later, not blocking):** two refinements worth considering when
`gl_processor` is next touched — (a) make the `restated` test "posting_date **after** `close(P)`"
rather than "posting *month* > document *month*", so the discriminator is the close boundary itself,
not a calendar-month proxy; and (b) if the demo specifically wants to *exercise* a `restated` state,
move the generated true-up's posting date to **after June 8** (it currently posts June 6) — that's a
one-line `generate_snapshots.py` change, separate from processor logic. Both are deferred; today's
decision keeps the current code and data as-is.

### Frozen close reference
**Chose:** On the transition to `closed`, persist the period's settled metric values as the
baseline the **Period Restatement** alert measures against; a later `restated` recompute compares to
*that frozen reference*, not to a re-derived figure.
**Why:** "Spend changed after close by X%" is only meaningful against the number that was
authoritative *at* close. Re-deriving the baseline each run would let the reference drift and make a
restatement undetectable. A persisted baseline is the honest anchor.

### Projectability contract: resolved once, consumed downstream
**Chose:** `metrics_calculator` resolves `gl_completeness_state` once and emits a boolean
`is_projectable` (true **iff** `open`). `projection_engine` reads it and **never re-derives** the
state.
**Rejected:** Letting `projection_engine` independently re-evaluate completeness/close logic.
**Why:** One owner of the state means the projection layer can't disagree with the metric layer
about whether a period is open — a single source of truth for "is this still in the proactive zone."
Projection on a closed/restated period is meaningless (it's settled), so the flag also enforces §6.

### Weighted projection = trailing-21-day linear regression
**Chose:** The weighted projection is a **least-squares line fit over the trailing 21 days** of
daily values, slope extrapolated to period end; falls back to **all available data** when the
period is younger than 21 days.
**Rejected:** The earlier "weighted recent average" / "weight recent days more heavily than older."
**Why:** A weighted average still answers "what's the level lately," not "where is the trend
heading" — and the proactive signal is fundamentally about **trajectory**. A regression slope
captures direction directly and degrades gracefully on short windows. Supersedes §6's prior wording
(a [LOCKED] item, revised deliberately).

### LTV hierarchy: calculated first, plan as fallback
**Chose:** Resolve LTV in this order — **calculate first, fall back to plan**: (1)
`calculated_retention` (primary) = trailing-3-month avg margin × `expected_retention_periods` (needs
≥3 months history); (2) `calculated_term` = margin × `contract_term_months`, **only when retention
is unconfigured** and therefore **Term-only** (no term for Month_to_Month); (3) `plan_input`
(`ltv_ref`) fallback, including first run; (4) `unresolved` — deferred, labeled rather than blanked,
when even plan is unavailable (a brand-new segment with no plan row).
**Rejected:** A plan-first chain (briefly drafted, then reversed by the owner); dropping a value
entirely when no method applies.
**Why:** LTV is a *calculated* metric by design (§10 — `avg_margin_per_period ×
expected_retention_periods`); the plan figure is the safety net, not the headline. When the inputs
exist, the computed value is the more truthful one and should lead; plan only stands in when history
or config is missing. `calculated_term` is a deliberate stopgap for Term segments lacking retention
config; it cannot serve Month_to_Month, so the chain falls to `plan_input` and finally an explicit
`unresolved` label (§9). This **preserves** the original §10 "Calculated (primary)" direction
(detailing its method tiers), rather than reversing it.

### Pro-rating: per-unit calendar_days (default) vs business_days
**Chose:** Plan pro-rating for partial periods is a **per-unit switch** — `calendar_days` (default)
or `business_days` — owned by `projection_engine`.
**Why:** Reaffirms §6; recorded here as a BS3 planning decision so the per-unit (not global) grain
is unambiguous when `projection_engine` is built. Weekday-only channels (the generator already
zeroes weekend activity) pro-rate on business days; everyday operations on calendar days.

### Severity and the estimated flag are orthogonal
**Chose:** `risk_level` (HIGH/MEDIUM/LOW/INFO) reflects **magnitude only**. A separate boolean
`estimated` carries data confidence — true for any non-`real` metric method or any open-period
projection. The two never interact: an **estimated HIGH stays HIGH**; low confidence is shown via
`estimated` + the confidence indicator, never by downgrading severity.
**Rejected:** Folding confidence into severity (e.g. demoting an estimated HIGH to MEDIUM).
**Why:** Conflating "how bad" with "how sure" hides real risk — a HIGH variance built on an
extrapolated CPA is still a HIGH variance worth surfacing. Keeping the axes separate lets the
narrative say "serious, and here's how confident we are" instead of silently muting the alarm,
which is exactly the never-cry-wolf-but-never-go-quiet balance (§4, §9).

### Findings grain is metric-driven; never collapsed
**Chose:** Each finding keeps the honest grain of its metric — CPA / CPA-vs-LTV / projection at
**unit** `(entity, region, segment)`; margin / fallout at **leaf** (full hierarchy). One finding
per flagged condition (a segment with three issues → three findings). The feed rolls up to the unit
for *display* only; the stored findings retain native grain.
**Rejected:** Coercing all findings to one shared grain; merging a segment's issues into one row.
**Why:** GL carries no product/customer dimensions, so a leaf-grain CPA would fabricate precision
the ledger doesn't have (consistent with the BS3 merger decision); conversely margin/fallout *are*
leaf-real and shouldn't be blurred up. Per-condition findings keep each issue independently
explainable and rankable by the LLM.

### metrics_calculator internal order: COGS → margin → LTV → CPA → fallout
**Chose:** Compute in dependency order — COGS first, then margin (needs COGS), then LTV
(`calculated_retention` needs margin), then CPA, then fallout. Every trailing-average path needs
≥3 months of history or falls back to plan, labeled.
**Why:** A fixed order means each metric's inputs already exist when it runs — no forward
references, no recomputation. Documented now so module 1's structure is settled before code.

---

## 2026 — Build Sequence 3 (analytics core — module 1: metrics_calculator, build-time)

> Surfaced while building `app/analytics/metrics_calculator.py` and walking the demo-arc snapshots.
> Each refines a recently-recorded planning decision; the reasoning is here, the docs (§6/§10/§14)
> updated to match.

### `is_projectable` is a calendar fact (current period), not `gl_completeness_state == "open"`
**Chose:** `is_projectable = (period == the snapshot's month)` — the single period still accumulating
toward its end — emitted identically for the unit (CPA) and leaf (economics) frames.
**Rejected:** The planning decision's literal `is_projectable = (gl_completeness_state == "open")`.
**Why:** The CLI walk showed `gl_processor` marks a **prior** month still inside its settlement grace
as `open` (e.g. April at a May-1 snapshot, since April closes May 8). That month is *over* — projecting
it to "period end" is meaningless. Conversely a current-month unit with **no GL posted yet** has
`state == NaN` but is plainly projectable. So GL-completeness (a spend-settlement concept that spans
months) is the wrong basis for projectability (a "is this period still open" concept). The calendar
test captures the intent exactly and makes unit and leaf agree. Supersedes the planning wording in
§6/§14.

### `unit_economics_flag` is dropped from metrics_calculator (risk_classifier owns it)
**Chose:** metrics_calculator does **not** emit `unit_economics_flag`; it supplies the inputs (T12M CPA
in the `cpa` frame, LTV in `economics`). `risk_classifier` computes the §11 CPA-vs-LTV / unit-economics
inversion alerts where the thresholds live.
**Rejected:** Computing the literal §14 `CPA + COGS_per_unit > price_per_unit` in metrics.
**Why:** That literal form is dimensionally inconsistent — CPA is **per-acquisition** (one-time per
customer, $65–161 in the data) while price/COGS are **per-period unit** rates ($58–94) — so it fired on
~90% of unit×periods including calm baselines (cry-wolf). Amortized to a common lifetime basis it reduces
algebraically to `CPA > LTV`, i.e. it *is* the CPA-vs-LTV inversion. So the per-period flag is either
broken or redundant; the honest home for the inversion is the threshold layer. §14 keeps the field,
populated downstream.

### CPA open-period label named `gl_partial` (not `gl_extrapolated`); `cogs_method` gains `actual`
**Chose:** The open-period to-date CPA label is **`gl_partial`**; the time-varying current COGS label is
**`actual`** (added to the `cogs_method` enum).
**Why:** With full-period scaling owned by `projection_engine` (planning decision 1), the metrics-layer
open value is the period-to-date figure, not an extrapolation — `gl_extrapolated` was a misnomer.
`actual` distinguishes the current effective COGS rate from the `plan_input` / `trailing_avg` / `estimated`
fallbacks now that COGS is time-varying. Both reconcile §14's enums with the implemented behavior.

### Observation (not a change): today's data has no plan-vs-actual COGS delta
**Noted:** the time-varying/effective-dated COGS machinery is built and correct, but on the current
snapshots `cogs_actual == cogs_plan` everywhere and every row resolves to `actual`. The leaves with later
`effective_date`s (Direct_Mail West 2024-03, Telemarketing West 2024-05-15) are **new segments** whose
periods all begin at/after their effective date, so no period falls back to `plan_input`, and config==plan
by construction (the revision-6 invariant). The COGS-delta path will exercise the moment the generator
emits a rate change that diverges from a previously-set plan — a future generator tweak, not a code gap.

---

## 2026 — Build Sequence 3 (analytics core — module 2: projection_engine)

> Decided in planning + confirmed while building `app/analytics/projection_engine.py`. Refines the
> §6/§11/§14 projection model (a [LOCKED] area), so the changes are deliberate; docs updated to match.

### Projection differs by metric: volume projects, CPA does not
**Chose:** Project **volume** (activations/submissions) two ways — linear + trailing-21-day cumulative
regression. **Do not project CPA**: surface its **current run rate** (spend-to-date CPA, already
`cpa_monthly`/`gl_partial`) paired with a **month-end estimate from prior months** (trailing CPA,
already `cpa_t3m`/`trailing_avg`) or a **no-history** state.
**Rejected:** Projecting every metric "two ways" (the original §6 reading), incl. a daily regression on CPA.
**Why (owner's call + engineering case):** CPA is ledger-driven and the ledger posts on an invoice
cadence (weekly/monthly), not daily — there is no smooth daily CPA signal to regress, and a regression
on spiky invoice data would track invoice timing, not economics. Volume *is* a smooth daily signal, so
it projects cleanly. For CPA the honest proactive signal is *current run-rate vs the historical norm*
(e.g. hero May run-rate ~147 vs historical ~117), which needs no forward projection — you can't project
ledger spend you don't yet have. Supersedes the §6/§11/§14 "both methods for everything" wording.

### Weighted projection regresses the cumulative series, not daily increments
**Chose:** Fit the trailing-21-day least-squares line to the **cumulative** daily series; slope = recent
per-day run-rate; `proj = to_date + slope × days_remaining`. Fall back to all-available days (<21
elapsed), then to the linear line (<2 points / degenerate).
**Rejected:** Regressing daily *increments* (the literal "21 daily values").
**Why:** Cumulative is monotonic and well-behaved even when daily activity is bursty; its OLS slope is
≥0, so the projection can never fall below the to-date value. The linear line is always computed as the
simple, always-works backup (owner: "a backup to always have").

### projection_engine emits values; risk_classifier computes variances
**Chose:** Output projected values + plan targets (full-period `*_plan_full` and pro-rated
`*_plan_prorated`); `risk_classifier` computes every `variance_pct` and assigns risk.
**Why:** Same separation used for `unit_economics_flag` — values vs. thresholds live in different modules.

### Pro-rating default is calendar_days; activations correctly land every day
**Chose:** `pro_rate_default` (`calendar_days`) drives the day-count basis for all units; `business_days`
is a built-but-unmapped per-unit seam (no per-unit config table yet).
**Why:** Even a weekday-only *submission* channel **converts on weekends** (a weekday sale converts ~2
days later, landing any day — verified: Door_to_Door has Sat/Sun conversions), so `calendar_days` is
correct for the **activation** projection. `business_days` matters mainly for *submissions* of
weekday-only channels — deferred until a per-unit pro-rate config exists.

### projection_engine does not emit a CPA frame (CPA paired downstream)
**Chose:** Output only the leaf-grain `volume_projection` frame. CPA's run-rate + month-end estimate are
read from the metrics `cpa` frame and paired by `findings_builder`, not recomputed here.
**Why:** Keeps "no CPA math in projection" literal and avoids duplicating values metrics already owns.
**Status:** Flagged for confirmation at plan approval; approved.

---

## 2026 — Build Sequence 3 (analytics core — engineered COGS anomaly)

### A COGS anomaly added to the generator (Online_Partner ERCOT North)
**Chose:** Engineer a standalone COGS-spike / margin-compression beat by adding a `cogs_anomaly`
field to the `Series` roster (`scripts/generators/shared.py`) and emitting a **second,
effective-dated `cogs_config` row** for the target unit in `canonical_cogs_config_rows()`. Target:
**Online_Partner, ERCOT North** (a calm channel with *no* CPA spike, and with a price so margin is
computed) — actual COGS steps **+22%** (31.0 → 37.82) effective **2024-05-15**, while the plan
`cogs_ref` stays flat at 31.0. So May reads `cogs_actual 37.82` vs `cogs_plan 31.0` (+22%) and
margin compresses ~25% (27.0 → 20.24); April and prior stay calm.
**Rejected:** Putting it on Web_Direct (the docs' "online advertising" channel — the user excluded
it) or compounding it onto a CPA-spike segment (Door_to_Door / Telemarketing) — keeping the COGS and
CPA stories on *separate* segments so the narrative can attribute each cleanly.
**Why:** Until now `cogs_actual == cogs_plan` everywhere (the revision-6 one-source invariant), so the
entire COGS alert family + `cogs_comparison_mode` machinery had **no signal** to fire on. This gives
the COGS-spike alert (and a margin-compression beat) a real, isolatable demo signal, and exercises
the time-varying / effective-dated COGS path built in `metrics_calculator`.
**Mechanism notes:** the anomaly lives **only** in `cogs_config` (a config table) — `gen_reference`'s
`cogs_ref` is untouched — so **the committed snapshot feeds do not change** (only `config/cogs_config.csv`
gains 3 rows, one per leaf of the unit). The validator builds its leaf roster as a *set*, so the second
row per leaf is harmless; `metrics_calculator` resolves the latest effective rate ≤ period-end.
**Supersedes** the earlier observation (BS3 module-1 entry) that "today's data has no plan-vs-actual
COGS delta."

---

## 2026 — Build Sequence 3 (analytics core — module 3: risk_classifier)

> Decided in planning + confirmed while building `app/analytics/risk_classifier.py` and walking the
> arc. Thresholds live in `config/system_config.yaml`; doc §11/§14 reconciled.

### Score every metric into one normalized assessment table (LOW included)
**Chose:** Emit one row per metric × period × grain — *every* metric gets a risk level (HIGH/MEDIUM/
LOW/INFO), including LOW/on-track. The feed filters; nothing is suppressed.
**Rejected:** Emitting only threshold crossings.
**Why:** The owner wants full transparency / browsability ("see or double-click into the data"). A
complete, uniformly-shaped assessment table also gives `findings_builder` everything it needs and lets
the UI show "what's off" while still letting a user inspect the calm metrics behind it.

### Finest honest grain; per-event roll-up deferred to findings_builder
**Chose:** Classify at the finest grain each metric's data supports — **leaf** for COGS/margin/fallout/
volume, **unit** for CPA & CPA-vs-LTV (GL only resolves to unit). Each row carries `group_key`
(entity|region|segment); the roll-up of a multi-leaf event into one feed alert with leaf drill-down is
`findings_builder`'s job.
**Why:** Matches the metric-driven-grain rule and the owner's "most granular calculation possible," while
keeping the feed digestible (one COGS-anomaly event = 3 leaf rows → one rolled-up alert downstream).

### CPA-vs-LTV: compression on T3M, inversion on T12M
**Chose:** Compression (MEDIUM, ≤ MEDIUM cap) on **trailing-3-month** CPA / LTV ≥ 0.80; inversion (HIGH)
on **trailing-12-month** CPA / LTV ≥ 1.0.
**Rejected:** Compression on T12M (the prior §11 wording).
**Why:** Verified in the data — Door_to_Door North sits at **T3M/LTV = 0.846** (crosses, fires) but
**T12M/LTV = 0.766** (a year of history swamps one month's spike, never crosses). T3M is the responsive
basis the proactive signal needs; T12M is the slow-burn unit-economics guardrail. Resolves the open
question; supersedes a [LOCKED] §11 line (revised deliberately).

### Fallout fires on degradation vs the channel's OWN trailing baseline (resolved cohorts only)
**Chose:** Band fallout on the **relative rise over a trailing-3-month (prior) baseline** per leaf,
**only for resolved cohorts**; threshold MEDIUM 0.40 / HIGH 0.70 (above monthly noise ~20–45%).
**Rejected:** Absolute fallout threshold (every channel's resolved rate is similar ~13–20% — Telemarketing
only stands out *relative to its own history*); fallout-vs-plan (every channel runs ~45–110% above its
optimistic plan → fires everywhere = cry-wolf); banding pending cohorts (inflated by not-yet-landed
conversions — a lagging signal that only resolves post-close, §8).
**Why:** The engineered fallout channel (Telemarketing) degrades to **+85–139% above its own trailing
norm** and fires HIGH **at June-8 when the May cohort resolves** — exactly the lagging-signal beat — while
calm channels stay LOW. A pending current cohort is scored LOW (with the flag) until it settles, so Athena
never cries wolf on an unresolved outcome.

### First-run volume_miss is not a hard alert
**Chose:** A leaf with no prior-period history (launched this period) is **not banded** for volume_miss —
it stays LOW with a `first_run` note.
**Why:** A mid-period launch (e.g. Telemarketing West, live May 15) has a *full-month* plan but only
post-launch actuals, so the projected "miss" (~−76%) is a spurious comparison, not a real shortfall (§9 —
first run is labeled, never alarmed). Without this guard it fired a false HIGH.

### Severity is magnitude-only; estimated flips real across close (the honesty beat)
**Chose:** Risk level = magnitude only; a separate `estimated` boolean (non-`real`/`calculated` method or
any projection). An **estimated HIGH stays HIGH**.
**Why (and the payoff):** At **May-22** the CPA spikes are HIGH **and `estimated=True`** (open-period
`gl_partial`); at **June-8** the same spikes are HIGH **and `estimated=False`** (settled `real`). Athena
warns three weeks early *and labels exactly how sure it is* — never muting the alarm for low confidence.

### Restatement / frozen_reference derived statelessly (Option B)
**Chose:** Per the owner's Option B — `frozen_reference` = period CPA on spend posted on/before close;
`restatement_delta` = current − frozen = `late_invoice_amount / conversions`. Computed each run from
`gl_states`; no persistence.
**Why:** Keeps the whole pipeline stateless/recomputed-every-run (the owner's preference) and still
surfaces the late-April **accrued** update (Door_to_Door North, +7.8% CPA impact, MEDIUM) in-window.
**Status (open):** the demo has no *post-close* restatement (the May true-up posts June 6, before the
June-8 close → `accrued`, not `restated`); the restatement state is exercised only if the generator posts
a true-up after close. And the COGS-spike `linear_trend` baseline currently includes the current month
(dampens a fresh step to MEDIUM, ~+13.7% vs trailing, while margin-compression carries the HIGH); a
prior-period baseline is a noted refinement.

---

## 2026 — Build Sequence 3 (analytics core — proactive fallout + flag-don't-suppress)

### Fallout is projected proactively from the resolved sub-cohort (lag-corrected)
**Chose:** Make the current period's fallout **proactive** — `projection_engine.project_fallout`
estimates it from the **resolved sub-cohort** (sales older than the conversion-lag SLA, whose outcome
is final); `risk_classifier` bands that vs the channel's trailing-3-month baseline. Confidence scales
with the resolved fraction; below `MIN_RESOLVED_SALES` it falls back to the plain rate, labeled
`plain_no_data` / `no_data`.
**Rejected:** (a) Banding **only resolved cohorts** (the prior build) — makes fallout purely *lagging*
(fires at June-8), which "can't be proactive" (owner). (b) The owner's first instinct, **regress the
numerator and denominator** to period-end and divide — the **numerator (`unmatched`) is lag-biased**: a
sale from the last few days simply hasn't had time to convert, so cumulative `unmatched` is inflated at
the very edge a regression leans on → it would *over-project* fallout (cry-wolf, the opposite failure).
**Why:** The resolved sub-cohort is lag-free and available early. Verified: at **May-22** Telemarightl
flags **+50–106% over its own baseline (MEDIUM/HIGH), with high confidence, weeks before close**, then
escalates as the cohort resolves — exactly the proactive signal. This is the right answer to the owner's
"it can't be proactive if it can't use a trailing average — get this one right."
**Status (open):** at *leaf* grain the resolved sub-cohort is small and noisier (a calm leaf can show a
spurious +60%); confidence is keyed to the resolved *fraction*, not the sample *count* — folding sample
size into confidence is a noted refinement.

### Flag-don't-suppress (with a confidence label) replaces silent LOW-suppression
**Chose:** Per the owner — *flag everything at its magnitude with an explicit confidence/`estimated`
label, and refine the structural fixes later*, rather than silently dropping uncertain signals to LOW.
So: pending/projected fallout is **flagged** (carrying its confidence), and a first-run leaf's volume
miss is **flagged with `first_run` + low confidence** instead of suppressed.
**Rejected:** Suppressing first-run volume_miss and pending fallout to LOW (the prior build).
**Why:** The owner would rather *see every potential miss and review it* than have Athena hide one. This
stays honest about "never cry wolf" by carrying the uncertainty in the **label**, not by hiding the row.
**Status (open):** the first-run volume_miss comparison is still structurally imperfect (full-month plan
vs partial-month actuals → an overstated −76%); pro-rating the launch-month plan is the deferred fix.

---

## 2026 — Build Sequence 3 (analytics core — module 4: findings_builder)

### Non-LOW become §14 findings; the assessment table is the browse layer
**Chose:** `findings_builder` turns only the **non-LOW** (HIGH/MEDIUM/INFO) assessments into §14
structured findings (the feed); `compute_findings` returns `{"findings", "assessments"}` so the full
scored table (incl. every LOW/on-track row) rides alongside as the drill-down/browse source.
**Rejected:** A heavy §14 finding per metric incl. LOW (hundreds of objects the feed/narrative would
just re-filter).
**Why:** Keeps the feed digestible while preserving "see everything" — it lives in the table, not as
findings. The §14 finding is the contract the LLM/report consume; flooding it with on-track rows would
bury the signal and bloat the narrative inputs.

### One finding per (alert_type, unit, period); leaves nested for drill-down
**Chose:** Roll leaf-grain alerts up to the unit — max severity, the **worst leaf** as the headline —
nesting every leaf under `supporting_metrics.leaves`. The COGS anomaly is **one** `cogs_spike` finding
(3 leaves) + **one** `margin_compression` finding (3 leaves), not six rows.
**Rejected:** One finding per leaf (repeats one event; noisy feed).
**Why:** Implements the "calc at leaf, roll up the alert, drill-down to leaves" decision and §14's
"rolls up to unit for display, native grain retained for drill-down." Worst-leaf headline is
conservative (surfaces the worst); volume-weighting is an easy alternative if a unit-representative is
preferred later.

### Feed ranked severity → magnitude → recency; deterministic positional ids
**Chose:** Order findings by severity (HIGH>MEDIUM>INFO) → |magnitude| → current-period-first → period
→ unit/alert tie-break; assign `finding_id` `F-001…` in ranked order (deterministic across runs).
**Why:** Matches the owner's ranking choice and makes the feed reproducible.
**Status (open):** (a) under flag-don't-suppress, a **first-run** volume_miss (Telemarketing West,
−76%, low-confidence) ranks high by magnitude and can crowd above genuine HIGHs — a **confidence-aware
tie-break** (real/high-confidence before estimated/low within a severity) would help, deferred since the
owner specified severity→magnitude→recency. (b) `finding_id` is positional (re-rank renumbers); a stable
hash id is a later option if findings must be tracked across runs.
**Verified:** the feed leads with the demo HIGHs; `estimated` flips **True→False across close** (CPA
spikes/fallout: estimated warning at May-22 → confirmed real at June-8) — the headline honesty beat.

---

## Template for new entries

```
### <short title>
**Chose:**
**Rejected:**
**Why:**
**Status (if not final):**
```
