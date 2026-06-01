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

## Template for new entries

```
### <short title>
**Chose:**
**Rejected:**
**Why:**
**Status (if not final):**
```
