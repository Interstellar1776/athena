# Athena — Open Questions

> **Purpose:** Unresolved decisions, kept visible so they don't get silently buried. When one resolves, move it to `decisions_log.md` as a dated entry and delete it here.
>
> **Status key:** 🔴 blocking a build phase · 🟡 should decide before the relevant phase · 🟢 nice to resolve, not urgent.

---

## 🟡 How far does the LLM's causal reasoning go?
The balance is set in principle (hypothesis, not fact; data always shown — see decisions log), but the *operational* line is untested. When May-22 fires multiple HIGH alerts and an operational note mentions a campaign launch, how strongly should the narrative connect them? "The May 8 note about the campaign launch may explain this" vs. "This is driven by the new campaign." Decide empirically once you can see real narrative output in Phase 4. The prompt wording is the lever.

## 🟡 Does RAG earn its place, or is metadata filtering enough? (Phase 7)
The retrieval corpus (operational notes + GL descriptions) is small and highly structured — every note already has entity/segment/date. Vector embedding + semantic search may be *worse* and certainly more complex than filtering notes by matching entity/segment/date to the flagged finding. Don't include "RAG" because it's a good pitch word — prove it beats simple filtering on this corpus. Test both in Phase 7. (If semantic search wins on free-text note content, keep it; if not, "intelligent context retrieval" via filtering is still an honest pitch.)

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

## 🟡 Conversational query router — how does it decide which module to call? (Phase 8)
Mode 2's hardest part. Does the LLM pick from a fixed menu of analytics functions (tool/function-calling style), or does it generate a structured query that Python validates and runs? The first is simpler and safer; the second is more flexible and riskier. Lean simple first. This is the most experimental part of the system — don't let a pitch demo depend on it until proven.

## 🟢 Web framework
FastAPI backend is settled. Frontend: React (richer, more work) vs. HTMX (simpler, server-rendered, faster to ship solo). For a solo builder optimizing for a working demo, HTMX is worth serious consideration. Decide at Phase 9.

## 🟢 Embedding model (only if RAG survives the Phase 7 test)
Local (e.g. nomic-embed via Ollama) vs. API-based. Moot if metadata filtering wins above.

## 🟢 Multi-tenancy
Single-user for v1. Multi-tenant is a later concern — noted so it isn't forgotten, not a v1 question.
