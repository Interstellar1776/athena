# Athena — Business & Pitch

> **Purpose:** Market, GTM, and pitch material. Separate from the architecture doc because it changes on a different cadence and serves a different audience (judges, not builders). This is a **working scaffold**, not a finished pitch — you're new to pitch competitions, so sections are annotated with what they're for. Fill them as you learn the specific competition's format and judging criteria.
>
> **First step before polishing any of this:** find out the format. Most fall into two buckets — (a) **business-plan / market** competitions (judges weight TAM, GTM, defensibility, team) and (b) **demo-day / technical** competitions (judges want to see it work). The mix determines how much weight goes to this file vs. a live demo of Mode 1. When you know your competition, note it at the top here.

---

## Competition target(s)
*(Fill in: name, date, format, judging rubric if published. Tune everything below to the rubric.)*

---

## The problem
Organizations make operational decisions on data that's 4–6 weeks old. By the time a cost spike, fallout trend, or performance deterioration appears in a formal report, the window to act has closed. The reporting cycle — close, consolidate, build variance reports, review — structurally guarantees leadership is looking backward.

*(Pitch tip: open with a specific, visceral version of this from retail energy — a real-feeling number. "A $40/CPA drift in one channel, caught at month-end close, is $X of wasted spend that was visible on day 8.")*

## The solution
Athena monitors operational signals on a schedule and projects current trends forward against plan — surfacing risks before period close, before the report is built, before leadership has to ask. It pairs deterministic Python analytics (trustworthy numbers) with LLM reasoning (cause hypotheses, prioritization, plain-language explanation). A sidekick for both operators and analysts.

## Why now
LLMs make it possible to combine quantitative anomaly detection with qualitative narrative reasoning at low cost. The quantitative half existed for years (BI tools); the missing half was an assistant that could *explain and prioritize* in plain language. The technology just caught up to the problem.

## Why it's defensible
The moat is **not the model** — anyone can call an LLM. It's:
1. **Operational workflow understanding** — the metric logic, GL completeness states, estimation hierarchies that keep the system useful when data is incomplete. This is domain knowledge, not code anyone can copy.
2. **The trust architecture** — numbers are computed in Python and the LLM is *architecturally prevented* from producing a number (it can only reference Python-owned placeholders). "We don't audit the AI's math — we make math impossible for it." This is the line that makes a skeptical enterprise buyer believe you. It is not replicable by plugging ChatGPT into a spreadsheet.
3. **Proactive-by-design** — most BI is pull (you ask). Athena is push (it tells you). Different product category.

*(Of these, #2 is your sharpest differentiator. Most "AI for BI" pitches can't answer "but how do I trust the numbers?" — you can, architecturally.)*

## Go-to-market wedge
One vertical, one use case, done precisely. Reference implementation: retail energy operations (founder's domain). Transferable to any industry running complex operational workflows on delayed reporting — but the wedge is depth in one vertical first, not breadth.

## Market sizing
*(To build. For a business-plan competition this matters. Frame as: how many orgs run operational workflows on monthly/periodic close cycles and have an analyst function? Start bottom-up from retail energy — number of retail energy providers in deregulated markets — then show the adjacent verticals as expansion. Judges trust bottom-up over a top-down "1% of a $Xbn market" claim.)*

## Competition / alternatives
*(To build. Honest framing of: traditional BI tools — Tableau/Power BI — pull not push, no reasoning layer; spreadsheet-based variance reporting — the status quo, slow; generic "AI analytics" startups — usually can't answer the trust question. Show you know the landscape.)*

## Business model
*(To build. SaaS per-seat? Per-entity/segment monitored? Note it here as it forms.)*

## Team / founder
Mechanical engineer + self-taught Python (pandas, SQL, Snowflake, SAP HANA), MBA candidate (UT Austin McCombs, working professional, Dallas), with direct domain experience in FP&A, pricing analytics, variance analysis, and customer-acquisition analytics at a retail energy provider. The defensibility-via-domain-knowledge claim is credible *because* of this background — lean on it.

## The demo
Anchor on **Mode 1** (the proactive feed), walking the snapshot narrative arc: May 1 calm → May 8 early drift → May 15 building → May 22 confirmed HIGH alerts with AI narrative and recommended action. The "money moment" is showing that Athena flagged on May 8 what a traditional report wouldn't surface until close. *(See `athena_context.md` §8 for the arc.)* Keep Mode 2 (chat) out of a high-stakes demo until it's rock-solid.

---

## Open pitch questions to resolve
- What's the single number that makes the problem visceral? (cost of one missed signal)
- What's the one-sentence version? ("Athena is ___ for ___.")
- Which competition, which rubric, which emphasis?
