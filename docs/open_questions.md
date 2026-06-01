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

## 🟡 CPA-vs-LTV compression — which CPA basis does it evaluate? (Phase 3)
§11 defines the compression alert on **trailing-12-month** CPA (`T12M CPA > % of
LTV`). But T12M is dominated by a year of history, so a single month's spike
barely moves it: in the Phase-1 demo data the hero's T12M/LTV sits at ~0.76 (a
rising *watch*) and will not cross the 0.80 line from the May event alone, even
though in-month economics are clearly compressed (May CPA ≈ 0.96 of LTV) and a
trailing-3-month basis would cross cleanly. Decide in Phase 3: evaluate
compression on trailing-3-month CPA (responsive, fires at May-22, stays calm at
May-1) and/or keep T12M as a separate slow-burn unit-economics signal — and if
the former, update the §11 wording (a [LOCKED] item, so revise deliberately).
The Phase-1 data supports either direction; no regeneration needed.

## 🟡 Conversational query router — how does it decide which module to call? (Phase 8)
Mode 2's hardest part. Does the LLM pick from a fixed menu of analytics functions (tool/function-calling style), or does it generate a structured query that Python validates and runs? The first is simpler and safer; the second is more flexible and riskier. Lean simple first. This is the most experimental part of the system — don't let a pitch demo depend on it until proven.

## 🟢 Web framework
FastAPI backend is settled. Frontend: React (richer, more work) vs. HTMX (simpler, server-rendered, faster to ship solo). For a solo builder optimizing for a working demo, HTMX is worth serious consideration. Decide at Phase 9.

## 🟢 Embedding model (only if RAG survives the Phase 7 test)
Local (e.g. nomic-embed via Ollama) vs. API-based. Moot if metadata filtering wins above.

## 🟢 Multi-tenancy
Single-user for v1. Multi-tenant is a later concern — noted so it isn't forgotten, not a v1 question.
