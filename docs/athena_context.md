# Athena — Project Context

> **What this file is:** The single source of truth for Athena's goals and architecture. Drop it into a Claude Project (or point Claude Code at it) so you don't have to re-explain the project every session.
>
> **How it's organized:** Each section leads with a plain-language statement of intent, then the precise detail. Read the intent lines for the shape of the system; read the detail when you're implementing.
>
> **Companion files** (`/docs`):
> - `decisions_log.md` — why each significant choice was made. The *reasoning* behind anything marked **[LOCKED]** here lives there.
> - `open_questions.md` — unresolved decisions, kept visible.
> - `business.md` — pitch, market, GTM, defensibility.
> - `data_dictionary.md` — every field defined.
>
> **[LOCKED]** marks architectural commitments that should not be contradicted without a deliberate revision (and a corresponding entry in `decisions_log.md`). Everything unmarked is current direction, open to refinement.

---

## 1. The Problem

**Intent:** Most organizations make operational decisions on data that is weeks old. Athena closes that gap.

The reporting cycle across most industries looks the same: a period closes, finance consolidates actuals, analysts build variance reports, and leadership reviews what happened four to six weeks ago — then makes decisions about conditions that no longer exist. By the time a cost spike or performance deterioration shows up in a formal report, the window to act has closed.

Athena's answer: **proactive operational intelligence that surfaces risks as they emerge, before period close, before the report is built, before leadership has to ask.**

---

## 2. What Athena Is

**Intent:** A proactive operational intelligence platform that pairs trustworthy numbers with genuine reasoning. A sidekick, not a replacement.

Athena combines three things:

- **Deterministic analytics** — Python calculates every metric, variance, projection, and risk classification. This is the authoritative source of truth.
- **AI reasoning** — an LLM interprets those findings: it hypothesizes likely causes, decides what matters most, explains findings in business language, and recommends actions. This is where the judgment lives.
- **Proactive delivery** — findings surface automatically on a schedule. The user doesn't have to pull a report to learn something is wrong.

The product is a **web application** serving two users who share the same analytical core:

1. **Operational leaders** — open Athena and immediately see what's off, why it likely matters, and what to consider doing. No report-building required.
2. **Analysts / senior individual contributors** — use the same tool to investigate further and form a hypothesis *before* taking it to the BI team or running a deeper analysis.

**Athena is a sidekick.** It makes operators faster and analysts sharper. It does not replace BI teams, ERP, or analytical work — it surfaces what matters so the human can act or dig deeper, faster than the current cycle allows.

**What it is NOT:**
- Not a general-purpose AI assistant
- Not an autonomous agent
- Not a replacement for ERP, BI tools, or analytical teams
- Not a real-time monitoring system
- Not locked to any single industry

---

## 3. The Core Principle — Numbers Are Truth, Reasoning Is the Sidekick's Read

**[LOCKED]** *(rationale: `decisions_log.md`)*

**Intent:** The single idea the whole system protects. Python owns every number. The LLM reasons freely but can never be the thing that produces a number.

> **Python determines truth. The LLM interprets truth.**

**Python is responsible for:**
- All calculations, without exception — metric derivation (CPA, COGS, LTV, margin, variance %, fallout rate), joins, aggregations, business-rule application
- Risk-level classification and threshold evaluation
- Structured findings generation
- Input validation and reconciliation at ingestion

**The LLM is responsible for:**
- Hypothesizing likely causes (as an analyst's read, grounded in the data and context provided)
- Deciding which findings matter most and how to prioritize them
- Explaining findings in plain business language for an exec or IC audience
- Recommending operational next steps
- Answering conversational follow-up questions by reasoning about which data to look at

**The division of trust:**
- **Numbers are true.** Every number a user sees was computed in Python and is traceable to source. See §4 for *how* this is guaranteed.
- **Causes and recommendations are the sidekick's reasoning** — a sharp analyst's hypothesis, presented naturally. The LLM doesn't need to hedge every sentence, but its causal claims are reasoning, not measured fact, and the supporting data is always shown so the human can judge the leap.

This is the distinction that makes Athena trustworthy *and* useful: it neither hallucinates numbers nor neuters the reasoning that makes it worth using.

---

## 4. How Numbers Stay True — The Hallucination Guard

**[LOCKED]** *(rationale: `decisions_log.md` — this is the headline defensibility claim)*

**Intent:** Don't check the LLM's math after the fact. Architecturally prevent it from doing math at all.

Naively letting the LLM write numbers and then trying to "extract and re-verify" them is unreliable — the model writes "about 20%," "roughly $148," "a fifth higher," and fuzzy matching produces constant false positives, which violates the never-cry-wolf principle.

**The design instead: the LLM never emits a number.** It writes prose with named placeholder tokens, and Python fills them from the structured finding.

The LLM produces:

> "CPA in `{entity}` is running `{variance_pct}` above plan, on pace to reach `{projected_linear}` by month-end."

Python substitutes each token from the finding it already calculated. The model cannot hallucinate a number because it was never permitted to type one — it can only reference slots Python owns.

**Failure behavior:**
- If the LLM emits a placeholder that doesn't exist in the finding (e.g. `{made_up_metric}`), substitution fails **loudly and deterministically** — no fuzzy matching, no tolerance, no false positives. The output is flagged, not silently dropped.
- If the LLM writes a bare digit in prose anyway (violating the contract), that is caught by a simple rule: prose from the narrative layer should contain no raw numerals. A stray numeral is a flag.

**Pitch framing:** "We don't audit the AI's arithmetic — we make arithmetic impossible for it. Every number a user sees was computed in Python and traces to source."

*(Detailed module design happens when you reach the narrative-generation phase. The contract above is the locked part; the implementation is not yet built.)*

---

## 5. Two Interaction Modes — One Analytical Core

**[LOCKED]** *(the shared core is locked; Mode 2 is the harder, more experimental half — see note)*

**Intent:** A proactive feed that pushes intelligence, and a chat interface that lets users pull answers. Both grounded in the same Python truth.

### Mode 1 — Proactive Intelligence Feed
- **Trigger:** scheduled batch run (configurable interval, daily default)
- **Behavior:** Athena runs the full pipeline, detects variances against plan, surfaces ranked findings automatically
- **Experience:** you open Athena and the findings are already there
- **Primary user:** operational leaders who need to know what changed without building a report

### Mode 2 — Conversational Query Interface
- **Trigger:** a natural-language question from the user
- **Behavior:** the LLM reasons about which Python module(s) to invoke → Python calculates → the LLM synthesizes a grounded answer
- **Experience:** *"Why is acquisition cost up in the Midwest this week?"* → Athena routes to the variance engine, retrieves context, returns a grounded answer
- **Primary user:** both leaders asking follow-ups and analysts investigating before a deeper dive

> **Sequencing note:** Mode 1 is the safer, more demonstrable half and should anchor any pitch demo. Mode 2 (the router reasoning about which module to call) is meaningfully harder and is built last. Don't let the demo depend on it until it's solid.

---

## 6. What "Proactive" Means — Precise Definition

**[LOCKED]**

**Intent:** Not "something changed today." Rather "if current trends continue, we will miss plan by X% — here's the warning while you can still act."

On any given day within a period, Athena:

1. Takes actuals accumulated to date for the current period
2. Projects them forward to period end using two methods, shown side by side:
   - **Linear extrapolation:** pace-to-date ÷ days elapsed × days in period
   - **Weighted trend (trailing-21-day linear regression):** fit a least-squares line over the trailing 21 days of daily values, extrapolate the slope to period end. Falls back to all available data when the period is younger than 21 days. **[LOCKED]** *(supersedes the earlier "weighted recent average" / "weights recent days more heavily"; rationale in `decisions_log.md`)*
3. Compares that projected period-end figure against plan (or forecast) for the full period
4. Surfaces the variance as a finding if it crosses a configured threshold

The signal Athena always answers: *"If current trends continue, where will we end the period, and how does that compare to plan?"*

### Projection only runs on open periods **[LOCKED]**
Projection is the proactive question — it is meaningful **only while a period is open** (still
accumulating). A closed or restated period is settled: it has actuals, not a projection.
`metrics_calculator` resolves each period's GL-completeness state once and emits a boolean
`is_projectable` (true **iff** `open`); `projection_engine` reads that flag and never re-derives
the state. See §10 (period lifecycle) and §14 (`is_projectable`).

### Confidence indicators
Every projection shows a confidence indicator reflecting how much of the period has elapsed. Example: *"Projected month-end CPA: $142 (linear) / $138 (weighted) — based on 8 of 30 days. Low confidence."* Early-period findings are **never suppressed**. Low confidence is shown, not hidden; the user decides how to weight it.

### Plan pro-rating for partial periods
When comparing period-to-date actuals against a monthly-grain plan, the plan is pro-rated to the elapsed portion. Method is a per-entity/segment config switch:
- `calendar_days` — plan ÷ calendar days × days elapsed (default; businesses operating every day)
- `business_days` — plan ÷ business days × business days elapsed (weekday-only operations)

Falls back to `calendar_days` if unconfigured.

---

## 7. Industry Applicability

**Intent:** Industry-agnostic architecture; a single domain as the reference implementation.

The core loop — operational data → deterministic analytics → anomaly detection → AI-interpreted alerts — applies anywhere organizations run complex operational workflows and rely on delayed reporting.

| Industry | Operational signal | Example Athena alert |
|---|---|---|
| Retail Energy | CPA by acquisition channel | "Door-to-Door CPA is 22% above plan in ERCOT North — 3rd consecutive week" |
| SaaS | Trial conversion / churn by cohort | "Enterprise trial-to-paid conversion dropped 18% — cohort started 3 weeks ago" |
| Logistics | On-time delivery rate by lane | "Dallas → Chicago lane fallout trending 2x above baseline this week" |
| Retail | Inventory turnover by SKU/region | "SKU 4821 velocity down 31% vs. plan in Southeast" |
| Financial Services | Portfolio variance vs. benchmark | "Credit segment 3 delinquency trending 1.4x above seasonal plan" |

**The reference implementation uses retail energy** because the founder has deep domain expertise there — which gives the synthetic data structural realism and the analytics grounded intuition. It does not constrain the architecture.

---

## 8. Data Modes and Snapshots

**[LOCKED]**

**Intent:** The same codebase runs in dev, demo, and production with no code changes — only a config setting differs.

Controlled by `system_config.yaml`:

- **`snapshot` mode** (dev + demo): the pipeline reads pre-built dated snapshot folders, each a cumulative view of the data as it would have existed on a given date. This simulates the passage of time so Athena can demonstrate proactive intelligence across a narrative arc without live data.
- **`live` mode** (production): the pipeline reads a live source — DB query, API pull, or file drop. The data loader's output is identical in both modes: a clean, validated dataframe. Nothing downstream knows the difference.

```yaml
# system_config.yaml
data_mode: snapshot                   # "snapshot" or "live"
snapshot_date: "2024-05-22"           # which snapshot to load — ignored in live mode
snapshot_path: "data/snapshots/"      # ignored in live mode
live_data_path: "data/live/"          # ignored in snapshot mode
```

### Snapshot folder structure
Each folder holds a **cumulative** view of all actuals for the period up to that date — mirroring how a real pipeline pulls all period-to-date actuals on each run.

```
data/snapshots/
├── 2024-05-01/   sales.csv + conversions.csv (1 day — period just opened) + gl_actuals.csv + reference_data.csv
├── 2024-05-08/   sales.csv + conversions.csv (8 days cumulative — early trend)
├── 2024-05-15/   sales.csv + conversions.csv (15 days — MEDIUM alerts begin)
├── 2024-05-22/   sales.csv + conversions.csv (22 days — HIGH alerts confirmed, pre-close)
└── 2024-06-08/   sales.csv + conversions.csv (post-close — May fully settled, every gain landed)
```

### Snapshots are generated, not hand-built
`scripts/generate_snapshots.py` produces them. A parameterized script lets you tune the demo narrative — sharpen the CPA spike, add a COGS anomaly, introduce a restatement — without hand-editing CSVs.

### Demo narrative arc (reference implementation)
- **May 1** — on track, minimal alerts. Establishes Athena doesn't cry wolf.
- **May 8** — Door-to-Door CPA drifting up (Q2 field-sales push: rising commissions/incentives). MEDIUM projection alert, low confidence. Weighted trend shows it emerging.
- **May 15** — drift continues, a second channel shows volume fallout. Two MEDIUM, one approaching HIGH.
- **May 22** — CPA spike confirmed, fallout above threshold, CPA-vs-LTV compression fires. Multiple HIGH alerts. The narrative explains the likely cause, references the relevant operational note, recommends action. This is what Athena caught three weeks before close.
- **June 8 (post-close)** — the settled month. Books are closed (`period_close_day`) and every in-period gain has landed, so this is May's final actuals: the CPA and fallout that the May-22 projection warned about, now confirmed. Lets the demo contrast pre-close *projection* against final *actuals*. (Fallout, a lagging signal, only fully resolves here — by design, you can't confirm an outcome that hasn't landed.)

---

## 9. First Run and Sparse Data

**[LOCKED]**

**Intent:** Useful from day one. Never refuse to produce output because data is incomplete — produce the best available output and label it.

- Fewer than 3 months of historical CPA → fall back to plan CPA, labeled. No error, no blank.
- No prior-year data for `prior_year_same_period` COGS → fall back to `plan_vs_actual` automatically.
- Only 1–3 days of actuals → projections still calculated, confidence weighted heavily; the narrative leads with the low-confidence note.
- A brand-new entity/segment with no history → every metric falls back to its plan input, labeled `plan_input`, until history accumulates.

**The principle:** incomplete data produces estimated outputs with clear labels — never blank outputs or silent errors.

---

## 10. Metrics — Calculation Logic and Fallback Hierarchies

**Intent:** Every metric has a defined calculation and an ordered fallback chain. Every output is labeled with the method that produced it, so the user always knows whether a number is real or estimated.

### CPA — Cost Per Acquisition **[LOCKED]**
CPA and COGS are **separate metrics**, never conflated. CPA actual derives from GL spend data. The ledger (`gl_actuals`) is **dimension-free**: each entry carries posting date, document date, cost center (the channel), GL account (the expense type), amount, and vendor. The business resolution — gain channel + geography — is reconstructed via `gl_mapping`, keyed on `(cost_center, gl_account, vendor)`; **CPA is computed at the channel × geography (entity/region/segment) grain**, not per customer attribute. The gap between posting and document date enables automatic late-invoice / restatement detection without accrual flags.

**Period lifecycle — compute forever, label-and-freeze at close** **[LOCKED]** *(rationale: `decisions_log.md`)*

A period **never stops being computable.** Closing is a *label*, not a removal: CPA recomputes
whenever new spend lands, in any period, at any snapshot. The lifecycle is
`open → closed → (restated)`; `accrued` is the cross-period-posting marker. Close day =
`period_close_day` (8); a period is *past close* when `snapshot_date ≥` day 8 of the following
month. This gives full live-mode realism: a **March snapshot** sees Jan/Feb `closed` and March
`open`, with no special-casing.

| State | Condition | Treatment |
|---|---|---|
| `open` | Current month, before its close day | The **proactive zone** — partial; apply estimation hierarchy, show confidence, **project** (`is_projectable = true`) |
| `closed` | Past close, no new spend since close | **Authoritative.** On transition to `closed`, **freeze the settled metrics as the period's reference baseline.** No projection |
| `restated` | Past close, new spend appeared | **Recompute** and **flag the delta vs. the frozen close reference** (Period Restatement alert). No projection |
| `accrued` | Document date in a prior period, posting date in the current period | Prior-period cost landing in the open month — flag delta vs. original CPA |

**Frozen close reference [LOCKED]:** when a period transitions to `closed`, Python persists its
settled metric values as the baseline the Period Restatement alert measures against. A later
`restated` recompute is compared to *that frozen reference*, not to a re-derived figure.

**Evaluation order — `accrued` is checked before `restated` [LOCKED]:** per `(unit, period)`,
first match wins: `open` → `accrued` → `restated` → `closed`. Checking `accrued` first is
deliberate (see the worked case below).

**Worked edge case (the demo):** the **June-8 snapshot settles May**. The May true-up carries a
**May document date** and **posts June 6** — *before* May's close (June 8). It therefore arrived
within May's settlement window: a prior-period (May) cost landing in the open current month (June),
which is an **`accrued`** entry, **not** a `restated` one. `restated` is reserved for spend that
posts **after** a period's close (a genuinely settled month changing). Because the posting (June 6)
precedes the close (June 8), evaluating `accrued` first yields the accounting-correct label.
*(Consistent with the built `gl_processor`, which checks `accrued` before `restated`; see
`decisions_log.md` for the reasoning and the note that a true `restated` demo would require posting
the true-up after the close day.)*

**CPA estimation hierarchy** (applied in order when GL spend is incomplete):
1. Full period GL spend posted → **real CPA** (authoritative, no flag)
2. Partial spend → **extrapolated CPA** (scaled to full period on historical patterns, flagged estimated)
3. No spend yet → **trailing 3-month average CPA**, flagged estimated
4. No history → **plan CPA**, flagged estimated from plan

Both **monthly CPA** (operational variance — running hot this month?) and **trailing-12-month CPA** (unit economics — sustainable vs. LTV?) are calculated.

### COGS — Cost of Goods Sold **[LOCKED]**
COGS is a **plan input** at entity/segment level, not calculated from transactions, not at customer level. Configured in a reference table with a `product_type` sub-dimension (e.g. term vs. month-to-month in energy).

**Comparison modes** (per entity/segment): `linear_trend` (trailing N-month avg — SaaS/stable), `prior_year_same_period` (energy/retail/seasonal), `plan_vs_actual` (any cost plan), `hybrid` (plan-vs-actual primary, prior-year as context — most complete).

**Fallback:** current input → trailing 3-period average (flagged) → plan COGS (flagged).

### LTV — Lifetime Value **[LOCKED]** *(hierarchy detailed — rationale in `decisions_log.md`)*
**Calculate first, fall back to plan.** Resolved in this order, each output labeled with the method
that produced it:

1. **`calculated_retention`** (primary) — `trailing-3-month avg margin per period ×
   expected_retention_periods` (retention is a per-sub-segment config input; door-to-door retains
   differently than broker-acquired). Requires ≥3 months of margin history.
2. **`calculated_term`** — `margin × contract_term_months`, used **only when retention is
   unconfigured**. Cannot serve `Month_to_Month` (no term), so it is Term-only.
3. **`plan_input`** (fallback, including first run) — the plan dataset's `ltv_ref`.
4. **`unresolved`** — deferred fallback when even plan is unavailable (e.g. a brand-new segment with
   no plan row); the value is labeled `unresolved` rather than blanked (§9 — never silent).

Labeled `calculated_retention` / `calculated_term` / `plan_input` / `unresolved`.

### Margin **[LOCKED]**
```
Margin per unit   = price_per_unit (from conversions data) − COGS per unit (plan input)
Margin per period = margin per unit × conversions in period
```
If price per unit is unavailable, fall back to plan margin input. Always labeled.

---

## 11. Alert Stack

**[LOCKED]** — these are the core proactive alerts. All thresholds configurable.

**Acquisition:** CPA spike (monthly CPA > plan by %, HIGH) · CPA trend (rising N periods, MEDIUM) · Volume miss (projected activations below plan by %, HIGH) · Fallout rate (lost/in over threshold, MEDIUM)

**Unit economics:** CPA-vs-LTV inversion (T12M CPA > LTV, HIGH) · CPA-vs-LTV compression (T12M CPA > % of LTV, default 80%, MEDIUM) · Margin compression (margin % declining N periods, MEDIUM) · Unit-economics inversion (CPA + COGS > revenue per unit, HIGH)

**Cost:** COGS spike (vs baseline by %, HIGH) · COGS trend (rising N periods, MEDIUM) · Late invoice (posting after close, INFO) · Period restatement (prior CPA changed by % from late invoice, MEDIUM)

**Projection:** Period-end miss linear · Period-end miss weighted (both HIGH/MEDIUM by magnitude) · Plan-vs-forecast gap (divergence > %, MEDIUM)

### Severity and the estimated flag are orthogonal **[LOCKED]**
Risk level (`HIGH`/`MEDIUM`/`LOW`/`INFO`) is **magnitude only** — how far off plan, never adjusted
for data quality. A separate boolean **`estimated`** carries data confidence: it is `true` for any
non-`real` metric method (`gl_extrapolated`, `trailing_avg`, `plan_input`, `calculated_*`,
`unresolved`) **or** any open-period projection. The two are independent: an **estimated HIGH stays
HIGH.** Low confidence is surfaced via `estimated` + the confidence indicator (§6), **never by
downgrading severity** — silently demoting an estimated alert would hide a real risk and violate
the never-blank / never-suppress principle (§9).

---

## 12. Reference Data — Plan vs. Forecast

**[LOCKED]**

Plan and forecast share one schema, distinguished by `reference_type`:

| Type | Definition | Mutability |
|---|---|---|
| `plan` | Set during the planning cycle; the original baseline | Locked once the period begins |
| `forecast` | Rolling update using recent actuals + updated forward estimates | Updated periodically |

Athena can compare actuals vs. plan, actuals vs. forecast, or surface the plan-vs-forecast gap itself. All three are meaningful. `reference_type` keeps every comparison unambiguous.

---

## 13. Data Model

**Intent:** The tables the pipeline ingests. Full field definitions live in `data_dictionary.md`; this is the shape.

All facts/reference carry the same denormalized **dimension hierarchy**: entity (market) → region → service_territory · segment · product_type + contract_term_months (Term only) · customer_size_tier → customer_class (residential only). Full field definitions in `data_dictionary.md`.

- **`sales.csv`** — customer_key, sale_date, *(dimensions)* — record-level submissions (no outcome; a sale doesn't know its fate). The fallout denominator
- **`conversions.csv`** — customer_key, sale_date, conversion_date, *(dimensions)*, price_per_unit (nullable) — record-level gains; joins to `sales` on customer_key. **Fallout = submissions with no matching conversion** (anti-join), so it's only resolved once gains have landed
- **`gl_actuals.csv`** — posting_date, document_date, cost_center (+description), gl_account (+description), amount, vendor, description — **dimension-free** raw ledger; resolves to channel+geography via `gl_mapping` keyed on (cost_center, gl_account, vendor)
- **`reference_data.csv`** — date, *(dimensions)*, reference_type (plan/forecast), volume_in_ref, volume_converted_ref, cost_ref, cpa_ref, cogs_ref, ltv_ref, margin_ref
- **`operational_notes.csv`** — date, entity, region, segment, note_text, author (qualitative context, feeds RAG)

**Reference/config tables** (`/config`): `gl_mapping`, `retention_config`, `cogs_config`, `system_config.yaml`.

---

## 14. The Structured Finding — The System Contract

**[LOCKED]**

**Intent:** The dict that every module reads and writes. No module invents data outside it. Every metric field carries a method label so both the user and the LLM know how a value was produced. This is also what the narrative layer's placeholders resolve against (§4).

```python
{
    "finding_id": "F-001",
    "entity": "ERCOT",
    "region": "North",
    "segment": "Door_to_Door",
    "product_type": "Term",
    "metric": "cost_per_acquisition",
    "period": "2024-05",
    "days_elapsed": 8,
    "days_in_period": 31,
    "confidence": "low",                       # based on % of period elapsed
    "is_projectable": True,                    # true iff gl_completeness_state == "open" (§6); projection_engine reads this, never re-derives

    "actual": 142.00,
    "actual_method": "gl_extrapolated",        # real / gl_extrapolated / trailing_avg / plan_input

    "reference_value": 118.00,
    "reference_type": "plan",                  # plan or forecast

    "variance_pct": 20.3,
    "variance_direction": "UNFAVORABLE",
    "risk_level": "HIGH",                      # magnitude only — never adjusted for data quality (§11)
    "estimated": True,                         # orthogonal to risk_level: true for any non-real method OR open-period projection (§11)

    "projected_period_end_linear": 148.00,     # populated only when is_projectable
    "projected_period_end_weighted": 144.00,   # trailing-21-day linear regression, slope to period end (§6)

    "cogs_per_unit": 0.048,
    "cogs_method": "plan_input",               # plan_input / trailing_avg / estimated
    "ltv": 620.00,
    "ltv_method": "calculated_retention",      # calculated_retention / calculated_term / plan_input / unresolved (§10)
    "margin_per_unit": 18.40,
    "margin_method": "calculated",             # calculated / plan_input
    "unit_economics_flag": False,              # True if CPA + COGS > revenue per unit

    "gl_completeness_state": "open",           # open / closed / restated / accrued (§10 lifecycle)
    "frozen_reference": None,                  # settled metric values persisted at close; the baseline a restatement measures against
    "restatement_delta": None,                 # populated when restated: recomputed value − frozen_reference

    "supporting_metrics": {
        "volume_converted_actual": 310,
        "volume_converted_ref": 380,
        "cost_actual": 44020,
        "cost_ref": 44840
    },

    # Populated downstream
    "retrieved_context": "",
    "narrative": "",
    "validated": False,
    "validation_flags": []
}
```

### Findings grain is metric-driven **[LOCKED]**
Each finding carries the **honest grain of its metric** — granularity is never collapsed to a
single shared level:

- **Unit grain** `(entity, region, segment)`: CPA, CPA-vs-LTV, projection (GL resolves here; the
  ledger has no product/customer dimensions).
- **Leaf grain** (full 8-dim hierarchy): margin, fallout.

One finding per flagged condition: a single segment with three distinct issues produces **three
findings**, not one merged row. The proactive feed **rolls up to the unit for display**, but the
underlying findings retain their native grain for drill-down and for the LLM's reasoning.

---

## 15. System Architecture

**Intent:** One responsibility per module, structured output, clean interfaces. Build the full pipeline thin before deepening any layer.

### Analytics sub-modules — `variance_engine.py` orchestrates
```
app/analytics/
├── variance_engine.py     ← orchestrates, outputs structured findings
├── data_loader.py         ← reads snapshot or live source per data_mode
├── data_cleaner.py        ← nulls, dedup, normalization, bad-record flagging
├── data_merger.py         ← joins actuals↔GL↔reference, validates join integrity
├── gl_processor.py        ← applies gl_mapping, detects completeness state, flags late invoices
├── metrics_calculator.py  ← CPA/COGS/LTV/margin, applies fallback hierarchies, labels every output
├── projection_engine.py   ← linear + weighted (trailing-21-day regression) projections; reads is_projectable; pro-rates plan per-unit (calendar_days default | business_days)
├── risk_classifier.py     ← applies thresholds, assigns HIGH/MEDIUM/LOW/INFO (magnitude only); sets the orthogonal estimated flag
└── findings_builder.py     ← assembles structured findings, one per flagged condition, at each metric's native grain
```

**`metrics_calculator` internal order [LOCKED]:** `COGS → margin → LTV → CPA → fallout`. Margin
depends on COGS; LTV (`calculated_retention`) depends on margin; the chain runs in that order so
each metric's inputs exist before it computes. Every **trailing-average** path (CPA `trailing_avg`,
COGS `trailing_avg`, LTV `calculated_retention`) requires **≥3 months of history** or falls back to
the plan input, labeled accordingly (§9). `metrics_calculator` resolves `gl_completeness_state`
once and emits `is_projectable` for `projection_engine` to consume (§6, §14).

### Full module map
```
athena/
├── app/
│   ├── analytics/        (the 9 modules above)
│   ├── llm/
│   │   ├── narrative_generator.py   ← findings → placeholder prose → Python fills numbers
│   │   └── query_router.py          ← routes conversational queries to the right module
│   ├── retrieval/
│   │   └── context_retriever.py     ← retrieval over operational notes + GL descriptions
│   ├── validation/
│   │   ├── ingestion_validator.py   ← schema/type/join integrity at load
│   │   └── narrative_validator.py   ← enforces the no-raw-numbers contract on LLM prose
│   ├── reporting/
│   │   └── report_generator.py      ← structures output for UI and export
│   ├── orchestration/
│   │   ├── batch_pipeline.py        ← scheduled proactive run
│   │   └── query_pipeline.py        ← on-demand conversational run
│   └── utils/
├── data/        snapshots/ · live/ · processed/ · contextual/
├── config/      gl_mapping.csv · retention_config.csv · cogs_config.csv · system_config.yaml
├── scripts/     generate_snapshots.py
├── outputs/ · notebooks/ · tests/
└── docs/        athena_context.md (this) · decisions_log.md · open_questions.md
                 business.md · data_dictionary.md
```

> Note: the post-LLM module is named `narrative_validator.py` (not "hallucination_guard") to reflect the §4 design — it enforces a contract, it doesn't audit arithmetic.

### Proactive batch pipeline flow
```
Scheduled trigger
  → data_loader (snapshot or live → clean dataframes)
  → ingestion_validator (schema/type/join; HALT loudly on failure — never pass bad data down)
  → variance_engine (cleaner → merger → gl_processor → metrics_calculator
                     → projection_engine → risk_classifier → findings_builder)
       ↳ outputs: list of structured findings
  → context_retriever (attach relevant operational notes to flagged findings)
  → narrative_generator (findings + context → placeholder prose)
  → narrative_validator (fill placeholders from findings; flag any orphan token or stray numeral)
  → report_generator (assemble validated findings + narrative for the UI feed)
```

---

## 16. LLM Layer — Configurable Endpoint

**[LOCKED]**

**Intent:** Same code runs against Anthropic's API in production, local Ollama in dev, or a self-hosted model on-prem — only environment config changes.

```python
# the module never knows which provider it's talking to
import os
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "https://api.anthropic.com/v1/messages")
LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_MODEL    = os.getenv("LLM_MODEL")        # set per environment — never hardcode a model string
```

The model string is **always** set via `LLM_MODEL` in the environment, never hardcoded in code or enshrined in this doc — model names change, and a hardcoded one rots silently. The HTTP call pattern is identical across providers; provider-specific behavior lives in config.

---

## 17. Error-Handling Philosophy

**[LOCKED]**

**Intent:** Always tell the user what happened and why, in plain language. Never fail silently.

- **Batch pipeline failures** (bad data, missing files, join errors, LLM timeout): fail **loud and explicit**. The pipeline halts with a clear message of exactly what failed and why. An empty or partial feed is worse than an honest error — it creates false confidence.
- **Conversational query failures:** never a blank response or a stack trace. Return a plain-language explanation and any partial result. *"I calculated your CPA variance for ERCOT North, but the LTV comparison couldn't complete because retention data isn't configured for that segment."*

---

## 18. LLM Usage Guidelines

**Always provide to the LLM:** structured findings (authoritative numbers + method labels) · retrieved operational context · a constrained prompt with explicit output format · instruction to reference numbers only as placeholder tokens and to acknowledge estimated values.

**Never allow the LLM to:** perform calculations · emit raw numerals in narrative prose · determine risk levels or classifications · generate analysis ungrounded in findings.

**What the LLM is encouraged to do:** hypothesize likely causes from the data and context · prioritize which findings matter · explain in exec/IC-friendly language · recommend a specific next step · in chat mode, reason about which data to look at to answer the question.

---

## 19. Build Sequence

**[LOCKED — build in order, don't skip phases]**

1. **Data + config foundation** — init Git, create `/docs` (this doc + companions), write `data_dictionary.md`, build reference/config tables, write `generate_snapshots.py`, generate snapshots, manually inspect realism, confirm the demo arc is visible.
2. **Ingestion validation** — `ingestion_validator.py`; test halt-on-bad-data and clean pass-through.
3. **Analytics core** — sub-modules in order (loader → cleaner → merger → gl_processor → metrics_calculator → projection_engine → risk_classifier → findings_builder), then `variance_engine.py`; manually verify every field and method label.
4. **Narrative generation** — `narrative_generator.py` with placeholder pattern; test local Ollama then API; check grounding and estimate-acknowledgement.
5. **Narrative validation** — `narrative_validator.py`; test orphan-token detection and stray-numeral detection; confirm clean output passes without false positives.
6. **Batch pipeline + reporting** — `report_generator.py`, `batch_pipeline.py`; run end-to-end across all snapshots; walk the demo arc manually.
7. **Retrieval** — `context_retriever.py`; embed operational notes + GL descriptions; confirm retrieval actually improves narrative quality (interrogate whether vector search beats simple metadata filtering on this small corpus — see `open_questions.md`).
8. **Conversational query** — `query_router.py`, `query_pipeline.py`; test NL question → correct module → grounded answer; test partial-failure plain-language handling.
9. **Web interface** — FastAPI backend exposing pipeline outputs; intelligence-feed UI; snapshot-date selector for demo mode.

---

## 20. Working Principles

1. Python determines truth. The LLM interprets truth.
2. The LLM never emits a number — it references placeholder tokens Python fills.
3. Causes and recommendations are the sidekick's reasoning, shown with supporting data — not asserted as measured fact.
4. One responsibility per module, structured output.
5. Build the full pipeline thin before deepening any layer.
6. Bad ingestion data halts the pipeline loudly. Never analyze unvalidated data.
7. Every metric output carries a method label — real, estimated, or plan-derived.
8. Incomplete data produces labeled estimates, never blanks or silent errors.
9. The proactive feed pushes; chat pulls; both use the same Python core.
10. The LLM endpoint and model are always environment-configured, never hardcoded.
11. Industry-agnostic architecture, domain-specific reference implementation.
12. Proactive beats reactive — every design decision should serve that.
13. Athena is a sidekick, not a replacement.
