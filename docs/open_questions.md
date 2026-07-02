# Athena — Open Questions

> **Purpose:** Unresolved decisions, kept visible so they don't get silently buried. When one resolves, move it to `decisions_log.md` as a dated entry and delete it here.
>
> **Status key:** 🔴 blocking a build phase · 🟡 should decide before the relevant phase · 🟢 nice to resolve, not urgent.

---

## 🟡 How far does the LLM's causal reasoning go?
The balance is set in principle (hypothesis, not fact; data always shown — see decisions log), but the *operational* line is untested. When May-22 fires multiple HIGH alerts and an operational note mentions a campaign launch, how strongly should the narrative connect them? "The May 8 note about the campaign launch may explain this" vs. "This is driven by the new campaign." Decide empirically once you can see real narrative output in Phase 4. The prompt wording is the lever.

## ✅ RESOLVED (Phase 8) — Does RAG earn its place? → filtering stays the default
Ran the deferred bake-off for real against **extracted** (messy-transcript) notes. Built a genuine
semantic arm (`embeddings.py`: Ollama `nomic-embed-text` + numpy cosine) and implemented
`context_retriever`'s `strategy="semantic"` seam, then compared via `scripts/eval/bakeoff_retrieval.py`:
**filter 2/2, semantic 2/2** — semantic *matched* but did not *beat* filtering, at the cost of an
embedding model + per-run embeddings. The extraction pass re-imposes clean tags, so filtering is exact
and free. **Kept `strategy="filter"` default; semantic seam is real (not a stub) so a noisier future
corpus can flip it by re-running the bake-off.** See `decisions_log.md` (BS8).

## 🟢 Narrative validator — how strict is the contextual-number provenance check? (Phase 5 / 7 — mostly resolved)
The validator's rule is *provenance*, not digit-absence: a number in the narrative is allowed if it
traces to a Python-filled blank or appears verbatim in the retrieved context (see
`the_hallucination_guard.md` §4 / `decisions_log.md` BS4).

**Resolved (Phase 7):** the allowance is live with **exact-substring** matching and **flag-don't-drop**
(a stray that traces to neither a placeholder nor `retrieved_context` is surfaced, never silently
removed). See `decisions_log.md` (BS7). **Still open (lowered to 🟢):** a bare single digit (`"6"`) can
match coincidentally under exact-substring — tighten to phrase/window matching only if it bites in
practice; and whether to nudge the model to paraphrase storm dates as words. Decide empirically once we
see real local-model narratives quoting note numbers (Phase 8, with transcripts).

## ✅ RESOLVED (Phase 8) — Transcript → notes extraction: approach + test data
`note_extractor` turns raw transcripts into `operational_notes`-schema rows via an **LLM extraction
pass** (the spine-aligned default): the LLM assigns tags from a dimension **dictionary** fed into the
prompt, and any number stays **verbatim** in `note_text` (provenance guard discards ungrounded numbers).
**Synthetic transcripts** were added (`scripts/generators/gen_transcripts.py`) to exercise the untagged
path; the authored `gen_notes` rows stay as the ground truth the extractor reconstructs. Verified on
local `qwen3:14b` (the May-19 `~$9.8k` invoice survives verbatim). See `decisions_log.md` (BS8).

## 🟡 LLM call count / cost / latency per batch run
Not yet characterized. How many LLM calls does one batch run make — one per HIGH finding? One batched call for all findings? This drives both cost (the pitch claims "low cost") and latency (affects whether the feed feels live). Estimate during Phase 4 once the narrative call exists. Batching all findings into one structured call is likely cheaper and more coherent — test it.

## ✅ RESOLVED (Phase 3) — CPA-vs-LTV compression basis → trailing-3-month
Compression now evaluates on **trailing-3-month** CPA (responsive — fires at May-22, calm at May-1);
the hard inversion stays on **T12M**. §11 wording revised. See `decisions_log.md` (BS3 module 3).

## 🟢 Deferred from Build Sequence 3 (analytics core) — display/data refinements, not blocking
- **First-run launch-month plan pro-rating.** A mid-period launch (e.g. Telemarketing West, live May-15)
  has a *full-month* plan but partial actuals, so its `volume_miss` is overstated (~−76%). Flagged with
  `first_run` + low confidence today (flag-don't-suppress); the honest fix is pro-rating the launch-month
  plan in `gen_reference`. (Decided generator-side, deferred.)
- **True post-close restatement data.** The engineered May true-up posts June-6 (before the June-8 close),
  so it reads `accrued`, not `restated`; the restatement *alert* is implemented but dormant until the
  generator posts a true-up *after* close. The late-April **accrued** update does surface.
- **Confidence-aware display / ranking.** Decided (BS3): low-confidence early-period projections stay
  flagged (§6 never-suppress); the "calm May-1" beat is achieved by **display-layer de-emphasis**, to be
  defined when the report/UI is built. Ranking within a severity is by exceedance only (not confidence).
- **`finding_id` is positional** (re-rank renumbers) — fine for a stateless feed; a stable hash id is an
  option if findings need tracking across runs.

## 🟡 Conversational query router — how does it decide which module to call? (Phase 9)
Mode 2's hardest part. Does the LLM pick from a fixed menu of analytics functions (tool/function-calling style), or does it generate a structured query that Python validates and runs? The first is simpler and safer; the second is more flexible and riskier. Lean simple first. This is the most experimental part of the system — don't let a pitch demo depend on it until proven.

## 🟢 Web framework
FastAPI backend is settled. Frontend: React (richer, more work) vs. HTMX (simpler, server-rendered, faster to ship solo). For a solo builder optimizing for a working demo, HTMX is worth serious consideration. Decide at Phase 10.

## ✅ RESOLVED (Phase 8) — Embedding model
The semantic arm uses **local `nomic-embed-text` via Ollama** (env `LLM_EMBED_MODEL`) + numpy cosine —
no heavy deps, no API. Built for the bake-off (`app/retrieval/embeddings.py`); filtering won, so it's
the alternative strategy, not the default. API-based embeddings remain a future swap behind the same
`embed_texts` seam if ever needed.

## 🟢 Multi-tenancy
Single-user for v1. Multi-tenant is a later concern — noted so it isn't forgotten, not a v1 question.
