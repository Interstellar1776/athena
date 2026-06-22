# The Hallucination Guard — how Athena's numbers stay true

> **What this file is:** The canonical explainer *and* pitch artifact for Athena's narrative layer —
> the half of the product that turns deterministic findings into language a human reads. It expands
> `athena_context.md` §4 (the locked contract) into the full reasoning, the architecture that
> enforces it, and the lines to say it out loud. Read this when you want to *understand* or *pitch*
> the number-safety design; read §4 for the terse locked version.

---

## 1. The problem

Every Athena narrative states numbers an operator will act on:

> *"Cost-per-acquisition in ERCOT North door-to-door is running 20.3% above plan, on pace to reach
> $148 by month-end."*

Those numbers must be **exactly right** — an executive is going to make a decision on them. But large
language models are unreliable at arithmetic and will confidently produce a wrong "$148" that *reads*
authoritative. A finance product that occasionally states a confidently-wrong number is worse than no
product: it erodes the trust the whole tool depends on.

## 2. The design we rejected — "check after the fact"

The tempting approach: let the LLM write the numbers into its prose, then have Python read the prose
back, find the numbers, and verify them against the truth.

This loses, and it's worth knowing *why*, because the failure is instructive. The model doesn't write
a clean, parseable `$148`. It writes *"roughly $150,"* *"about a fifth higher,"* *"~$148,"* *"just
under $150."* Now Python has to (a) recognize all of those as "the variance figure" and (b) decide
whether each is *close enough* to the truth. Every fuzzy match is a chance for a **false alarm** — and
false alarms violate Athena's first principle, *never cry wolf*. Verifying numbers embedded in
free-form human language is a semantic, probabilistic problem, and probabilistic verification of the
thing that's supposed to be certain is a contradiction.

## 3. The design — the inversion

The move that makes the product defensible: **the LLM never types a number.**

It writes the sentence with **named blanks**, and Python fills them from the structured finding it
already computed:

> The model emits: *"CPA in `{entity}` is running `{variance_pct}` above plan, on pace to reach
> `{projected_linear}` by month-end."*
>
> Python substitutes each blank from the finding: `{variance_pct}` → `20.3%`, `{projected_linear}` →
> `$148.00`.

Most people picture a **Mad Libs template**: Python owns the sentence, the LLM fills a word. That *is*
robotic. Athena does the **opposite** — it **inverts the template**:

> **The LLM owns the sentence. Python owns the numbers (and how they're displayed).**

The model keeps full freedom over everything that *should* be free: structure (paragraph or bullets),
phrasing, which finding matters most, the likely cause, the recommended action, the tone. The *only*
constraint is that any number it refers to must be one of the blanks we offer it. The output reads
natural because the model genuinely authored it — only the numeric slots are frozen.

**Python owns formatting, too — not just the value.** It's not enough to own the number `142.0`;
Python also owns whether it renders `$142.00` or `142` or `142 dollars`. Otherwise the model
reintroduces inconsistency (one finding says `$142`, the next says "142 dollars"). Owning the
formatter guarantees every CPA reads `$142.00` and every variance reads `20.3%`, everywhere.

The line to hold: **metric numbers must be blanks; qualitative judgment stays free** — and numbers
that come from *provided context* (a date, a figure quoted from an operational note) are allowed when
they trace back to that source (see §4). There's no blank for "this is an estimate" — we simply *tell*
the model `estimated: true` and let it phrase the caveat in its own voice.

## 4. Why this makes verification trivial (the deep trick)

In the rejected design, checking meant *parsing numbers out of prose and matching them to a computed
truth* — semantic, fuzzy, false-alarm-prone. The inversion replaces correctness-matching with a
**provenance** check: every number in the final narrative must *trace to a source we control*. There
are exactly two legitimate sources:

1. **A Python-filled blank** — an authoritative metric the analytics engine computed.
2. **The retrieved context we handed the model** — e.g. an operational note. A number that appears
   *verbatim* in that provided text is grounded, not invented.

Any digit that traces to **neither** is a hallucination — flag it. This is still a mechanical,
deterministic check (does this digit-string appear in a known source?), not a semantic correctness
match, so it keeps the **no-false-positives** property. We didn't verify the *correctness* of every
number against a truth table; we verified its *origin*.

> **We didn't make the AI more accurate. We changed the problem from "is this number right?"
> (unanswerable in prose) to "where did this number come from?" (a lookup).**

**Two number classes, one rule.** *Metric* numbers (CPA, variance, projections) must always be blanks
— the model may never type them. *Contextual* numbers (a date, "two weeks ago," a figure quoted from a
snowstorm note) are allowed **only** when they trace to the provided context. A naive "reject any
digit" regex would wrongly kill a perfectly good sentence like *"...consistent with the ice storm that
closed the territory the week of the 14th, per the field note"* — exactly the kind of grounded,
logical context we *want*. Provenance lets that through while still catching an invented number.

**Until retrieval exists, the rule degenerates to the simple case.** In the thin build (stages 4–5,
before step 7), `retrieved_context` is empty — so source (2) is empty, and the only legitimate origin
is a blank. There, the check really is "every number must be a filled blank," and that simple version
ships first. Provenance is the extension switched on when retrieval starts attaching notes — which is
also *why* the simple check is safe to start with.

## 5. The pitch

> *"We don't audit the AI's arithmetic — we make arithmetic **architecturally impossible** for it.
> Every number a user sees was computed in Python and traces to source."*

That beats "we double-check the AI's math," because it promises a *structural* guarantee instead of a
best-effort one — and a structural guarantee is one you can **demonstrate**, not just assert.

## 6. The architecture that enforces it — three actors

| Actor | Stage | Responsibility |
|---|---|---|
| **Generator** | 4 (`narrative_generator.py`) | finding → blanks-prose. Talks to the LLM. One job. |
| **Validator** | 5 (`narrative_validator.py`) | blanks-prose + finding → filled prose, **or** a flag. Pure Python, no LLM. |
| **Orchestrator** | 6 (`batch_pipeline.py`) | runs the loop: generate → validate → on a flag, regenerate that one finding → re-check → after N tries, emit an honest labeled fallback. |

**The validator's real job is the *fill*, not merely the check.** Blanks-prose
(`"...{variance_pct}..."`) is not publishable — something must substitute Python's real, formatted
numbers into the blanks. That substitution *is* the validator. The two checks fall out of it for free:
a blank with no matching field is an **orphan token**; a bare digit that traces to neither a filled
blank nor the provided context is a **stray numeral** (a hallucinated number). Numbers grounded in the
retrieved context are allowed — the check is **provenance, not mere digit-presence** (§4). The
generator alone can never produce final output — the validator is what makes the numbers real.

**Why the validator stays a separate module** (instead of folding the check into the generator's
retry): isolation makes the guarantee **testable and demonstrable**. You can feed the validator
adversarial prose laced with stray numbers — *with no LLM in the loop* — and show it catches every
one. A guarantee you can demo on a slide beats one buried in generation code. (It's also CLAUDE.md's
"one responsibility per module" earning its keep.)

**Why the retry loop lives in the orchestrator, above both:** the generator's one job is talking to
the model; the validator's one job is enforcing the contract. The coordination that ties them
together — try, detect, regenerate, give up honestly — belongs to neither single-responsibility
module; it sits above both. On final failure it emits an honest, labeled fallback (the finding's facts
in a plain marked line) rather than publishing an untrusted sentence (§17 — never fail silently).

## 7. Local-first, model-swappable (how we build and ship)

Provider and model are **environment configuration** (§16), never hardcoded. **Three providers are
supported, and the design stays open to more:**

- **Local Ollama (the default)** — local Qwen for development: free, private, fast to iterate on, no
  API key.
- **Anthropic** — Claude via the official SDK (cloud; needs `LLM_API_KEY`).
- **OpenAI** — GPT via the official SDK (cloud; needs `LLM_API_KEY`).
- *…or similar* — a new provider is one more thin adapter, not a rewrite (see below).

`LLM_PROVIDER` selects which one; `LLM_MODEL` names the model; a `model=` override argument on the call
carries a future web-UI model picker with **zero rearchitecting** — the dropdown's choice passes
straight through.

The shape that makes "three (or more) providers" cheap: **one interface (`complete(system, user,
model=None)`) over N thin adapters.** Each adapter only has to talk to its vendor's API and return
text; everything upstream is provider-blind. This is the honest reading of §16's "identical HTTP
pattern across providers" — *nearly* identical, because the Anthropic Messages API, OpenAI's API, and
Ollama's native API differ in request/response shape, so each gets its own small adapter behind the
shared seam. (§16 names all three — Anthropic, OpenAI, Ollama — plus self-hosted, under the same
env-config mechanism.)
