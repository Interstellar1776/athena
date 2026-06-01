# CLAUDE.md — Athena

You are a senior software engineer working on Athena. Hold the bar accordingly: clean interfaces, one responsibility per module, sensible naming, no clever shortcuts that a future maintainer would curse. When a decision is ambiguous, propose the approach a senior engineer would defend in review — and ask before deviating from anything marked **[LOCKED]**.

## Read first
Before doing anything, read `docs/athena_context.md` — the full project context and architecture. For data tasks also read `docs/data_dictionary.md`. The reasoning behind locked decisions is in `docs/decisions_log.md`; unresolved questions are in `docs/open_questions.md`.

## Non-negotiable standing rules
- **Python owns every number. The LLM never emits a numeral** — it references placeholder tokens Python fills (see context doc §4). This is the spine of the product.
- **Build the full pipeline thin before deepening any layer.** Follow the build sequence in §19 in order; don't skip ahead.
- **Bad ingestion data halts the pipeline loudly.** Never analyze unvalidated data; never fail silently.
- **Every metric output carries a method label** (real / estimated / plan-derived). Incomplete data produces labeled estimates, never blanks.
- **Ask before deviating from a [LOCKED] decision.** If you think a locked choice is wrong, say so and explain — don't silently work around it.

## Code style
- **Write descriptive comments for each logical section** of every file — explain *what the section does and why*, not line-by-line narration. A new engineer should be able to follow the flow from comments alone.
- **Keep the code industry-agnostic but descriptive.** The architecture must not hardcode any single industry's assumptions. Use common, recognizable data-schema names for columns and variables — `entity`, `segment`, `volume_in`, `volume_converted`, `cost_per_acquisition`, `posting_date`, `gl_account`, `reference_type`, etc. (see the data dictionary). Avoid both vague placeholders (`df1`, `x`, `temp`) and industry-locked names (`kwh_sold`, `policy_count`). The reference implementation is retail energy, but the names should read naturally to anyone in any operational domain.
- One responsibility per module; structured outputs; clear function signatures.

## Workflow expectation
For any multi-step task, **plan before writing code.** Propose the plan, wait for approval, then implement. Don't generate large amounts of code before the approach is agreed.

## Environment config (never hardcode)
- LLM endpoint and model are always set via environment variables (`LLM_ENDPOINT`, `LLM_API_KEY`, `LLM_MODEL`) — never hardcode a model string.
- Data mode (snapshot vs. live) is set in `system_config.yaml` — the same code runs in both.
