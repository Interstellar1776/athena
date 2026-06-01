# Athena — Data Dictionary

> **Purpose:** Every field, defined. This is the contract for both the synthetic-data generator (`scripts/generate_snapshots.py`) and the ingestion validator. Build this out *before* writing analytics modules — define the data, then compute on it.
>
> **Status:** Seeded from the schema in `athena_context.md` §13. Tighten ranges, add constraints, and add any new fields as the generator takes shape. Mark fields the validator must enforce.
>
> **Conventions:** dates ISO `YYYY-MM-DD` · money as float in base currency unless noted · nullable means the field may be empty and downstream logic has a defined fallback (see context doc §9–10).

---

## actuals.csv
Operational volume and revenue per period/entity/segment. The spine of the system.

| Field | Type | Nullable | Definition | Validation / notes |
|---|---|---|---|---|
| date | date | no | Reporting period (the day this row's actuals are dated to) | Must parse; not in the future |
| entity | string | no | Geographic territory / product tier / business unit | Must exist in reference data |
| segment | string | no | Acquisition channel / cohort / lane / SKU | Must exist in reference data |
| product_type | string | yes | Sub-dimension (e.g. term / month_to_month); null where N/A | If present, must match cogs_config |
| volume_in | int | no | Units entering the funnel | ≥ 0 |
| volume_converted | int | no | Units that completed the desired action | ≥ 0; ≤ volume_in |
| volume_lost | int | no | Units that did not convert | ≥ 0; ideally volume_in − volume_converted |
| revenue_per_unit | float | yes | Revenue per converted unit; drives margin | ≥ 0 if present; null → plan margin fallback |

*Generator notes:* this is where the demo arc lives. The CPA spike is engineered by raising GL spend faster than `volume_converted` grows across snapshots; fallout by raising `volume_lost` relative to `volume_in`.

---

## gl_actuals.csv
Raw general-ledger spend entries. Joined to actuals via `gl_mapping`. Source of CPA actuals and late-invoice detection.

| Field | Type | Nullable | Definition | Validation / notes |
|---|---|---|---|---|
| posting_date | date | no | Date the entry hit the ledger | Must parse; not in the future |
| document_date | date | no | Invoice/document date — the period the cost covers | Must parse; gap vs. posting_date drives late-invoice + accrued detection |
| cost_center | string | no | Maps to entity/segment via gl_mapping | Must exist in gl_mapping |
| gl_account | string | no | Maps to spend category via gl_mapping | Must exist in gl_mapping |
| amount | float | no | Spend amount | No negative spend (validator sanity check) unless modeling reversals |
| vendor | string | yes | Vendor name | — |
| description | string | yes | GL line description; feeds retrieval/context layer | Free text |

*Generator notes:* to demo `restated`/`accrued` states, post an entry in a later snapshot with a `document_date` in an earlier period.

---

## reference_data.csv
Plan and forecast targets. Same schema, distinguished by `reference_type`.

| Field | Type | Nullable | Definition | Validation / notes |
|---|---|---|---|---|
| date | date | no | Reporting period | Must parse |
| entity | string | no | Matching entity | — |
| segment | string | no | Matching segment | — |
| product_type | string | yes | Nullable sub-dimension | — |
| reference_type | string | no | `plan` or `forecast` | Must be one of the two |
| volume_in_ref | int | no | Reference inbound volume | ≥ 0 |
| volume_converted_ref | int | no | Reference conversions | ≥ 0 |
| cost_ref | float | no | Reference spend (for CPA) | ≥ 0 |
| cpa_ref | float | no | Reference CPA | ≥ 0 |
| cogs_ref | float | no | Reference COGS per unit | ≥ 0 |
| ltv_ref | float | no | Reference LTV — fallback when calculated LTV unavailable | ≥ 0 |
| margin_ref | float | no | Reference margin per unit — fallback | — |

*Note:* `plan` rows are locked once a period begins; `forecast` rows update. The generator should emit at least `plan`; add `forecast` rows to demo the plan-vs-forecast-gap alert.

*Generator convention (Phase 1):* `plan` rows are dated to the first of the reporting month. `forecast` rows are dated to their **issue date** (e.g. 2024-05-10), not the period start — so a forecast "arrives" mid-period and only surfaces from the snapshot on/after that date (snapshots are cut cumulatively; see *Snapshot conventions* below). `reference_type` still disambiguates the row; consumers comparing actuals vs. a reference should select by `reference_type` and the reporting period it targets, not assume `date` is the period start.

---

## operational_notes.csv
Qualitative commentary. Feeds the context/retrieval layer and is what lets the narrative reference a likely cause.

| Field | Type | Nullable | Definition | Validation / notes |
|---|---|---|---|---|
| date | date | no | Note date | Must parse |
| entity | string | yes | Relevant entity, or `ALL` | — |
| segment | string | yes | Relevant segment, or `ALL` | — |
| note_text | string | no | Free-text operational commentary | The retrievable content |
| author | string | yes | Note source | — |

*Generator notes:* seed a note dated ~May 8 about a campaign launch in the channel whose CPA spikes — this is what the May-22 narrative connects to. This is also the test bed for the RAG-vs-filtering question (`open_questions.md`).

*Generator convention (Phase 1):* `entity`/`segment` use the literal string `"ALL"` (not null) for organization-wide notes, so a note always carries an explicit scope. Validators/consumers should treat `"ALL"` as a wildcard that matches every entity/segment.

---

## Config / reference tables (`/config`)

### gl_mapping.csv
| Field | Type | Notes |
|---|---|---|
| cost_center | string | GL cost center code |
| gl_account | string | GL account number |
| entity | string | Maps to Athena entity |
| segment | string | Maps to Athena segment |
| spend_category | string | e.g. acquisition_marketing, overhead |

### retention_config.csv
Drives calculated LTV.
| Field | Type | Notes |
|---|---|---|
| entity | string | — |
| segment | string | — |
| expected_retention_periods | float | Average retention in periods |
| effective_date | date | When this input became active |

### cogs_config.csv
| Field | Type | Notes |
|---|---|---|
| entity | string | — |
| segment | string | — |
| product_type | string | e.g. term / month_to_month; nullable if N/A |
| cogs_per_unit | float | Cost per unit |
| cogs_comparison_mode | string | linear_trend / prior_year_same_period / plan_vs_actual / hybrid |
| effective_date | date | When this input became active |

### system_config.yaml
| Key | Type | Notes |
|---|---|---|
| period_close_day | int | Day after which a period is considered closed |
| pro_rate_default | string | calendar_days or business_days |
| batch_frequency | string | daily / weekly / custom cron |
| cpa_ltv_warning_threshold | float | Default 0.80 — triggers MEDIUM CPA-vs-LTV alert |
| min_confidence_display | string | always_show — low confidence shown, never suppressed |
| data_mode | string | snapshot or live |
| snapshot_date | date | Active snapshot — ignored in live mode |
| snapshot_path | string | Path to snapshots — ignored in live mode |
| live_data_path | string | Path to live data — ignored in snapshot mode |

---

## Snapshot conventions (generator)
Each snapshot folder (`data/snapshots/<date>/`) is a **cumulative** cut of the
full timeline — all rows dated on/before the snapshot date. The cut uses `date`
for `actuals`/`reference_data`/`operational_notes` and **`posting_date`** for
`gl_actuals` (so a late entry posted in May appears only from the snapshot on/
after its posting date, regardless of its earlier `document_date`). Prior-period
history is present in every snapshot, so trailing metrics work throughout. The
same four per-table files exist in every snapshot and can be emitted as `.csv`
and/or `.xlsx`.

## Generation checklist (Phase 1) — ✅ COMPLETE (see `build_log.md`)
- [x] Four snapshot dates (May 1 / 8 / 15 / 22), each cumulative
- [x] Demo arc visible: calm → drift → building → confirmed HIGH (context doc §8)
- [x] At least one operational note that explains the spiking channel
- [x] At least one late/accrued GL entry to exercise completeness states
- [x] Plan rows for everything; forecast rows for the plan-vs-forecast demo
- [x] A new entity/segment with no history to exercise the first-run fallback
- [x] Manually inspect: does it feel operationally real?
