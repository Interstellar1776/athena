#!/usr/bin/env python3
"""narrative_validator.py — fill the placeholders, prove no number was hallucinated.

Build Sequence §19 step 5 (context doc §4; design in ``docs/the_hallucination_guard.md``). This is the
second half of the spine: it takes the generator's **placeholder-prose** and Python-fills every number,
and in doing so enforces the contract deterministically:

* **Fill** — substitute each legal placeholder with its formatted value (``placeholders.render_placeholder``).
* **Orphan placeholder** — a ``{name}`` the model used that isn't legal for this finding (it can't be
  filled) → flagged.
* **Stray numeral** — a bare digit the model typed directly into prose (it should have used a
  placeholder) → flagged.

The check is what makes the pitch demonstrable: it needs **no LLM**, so it can be unit-tested against
adversarial prose and shown to catch every leak.

Two subtleties the algorithm gets right (see ``the_hallucination_guard.md`` §4):

* The stray-numeral scan runs on the **raw** placeholder-prose, *before* filling — after filling, the
  legitimate values *are* digits, so there'd be nothing to distinguish.
* It **ignores digits inside placeholder names** (``{cpa_t3m}``/``{cpa_t12m}`` contain ``3``/``12``)
  by blanking the ``{...}`` spans before scanning. Otherwise every such placeholder is a false positive.

Scope (this build): validator only. The regenerate-on-flag **retry loop** is step 6 (the orchestrator).
The **provenance** allowance (permit a digit that appears verbatim in ``retrieved_context``) is a
marked hook for step 7 — today ``retrieved_context`` is empty, so any bare digit is a stray.
**Known limitation:** number-*words* ("a fifth", "nearly double") are not caught — a digit check can't
see them and a word watchlist is too false-positive-prone; mitigated by ``numbers=withhold`` + the prompt.

CLI:
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.validation.narrative_validator --compare --out
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.llm.placeholders import PLACEHOLDER_RE, available_placeholders, render_placeholder

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A numeric run in prose: an integer with optional thousands-commas and an optional decimal, so
# "1,240" and "20.3" each count once and a trailing comma/period (sentence punctuation) is excluded.
_NUMERIC_RE = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")


# ===========================================================================
# 1. Result type
# ===========================================================================
@dataclass
class ValidationResult:
    """Outcome of validating one narrative. ``ok`` is the contract verdict; ``filled`` is always
    produced (legal placeholders filled, orphans left visible) so even a flagged narrative is readable."""

    filled: str
    ok: bool
    flags: list[str]
    orphans: list[str]
    strays: list[str]


# ===========================================================================
# 2. The pure core — fill + the two checks (no LLM, fully deterministic)
# ===========================================================================
def validate_narrative(narrative: str, finding: dict) -> ValidationResult:
    """Fill the placeholders in ``narrative`` from ``finding`` and flag any contract violation."""
    legal = set(available_placeholders(finding))

    # (a) Orphan placeholders — names the model used that this finding can't fill (dedupe, keep order).
    used = PLACEHOLDER_RE.findall(narrative)
    orphans = list(dict.fromkeys(name for name in used if name not in legal))

    # (b) Stray numerals — digits typed directly into prose. Scan the RAW text with the {...} spans
    #     blanked out, so digits *inside* placeholder names (e.g. {cpa_t3m}) are never flagged.
    stripped = PLACEHOLDER_RE.sub(" ", narrative)
    strays = _NUMERIC_RE.findall(stripped)
    # Provenance hook (step 7): once retrieval populates retrieved_context, allow a stray that appears
    # verbatim there, e.g.
    #     context = finding.get("retrieved_context") or ""
    #     strays = [s for s in strays if s not in context]

    # (c) Fill — legal placeholders become their formatted values; orphans stay visible as {name}.
    filled = PLACEHOLDER_RE.sub(
        lambda m: render_placeholder(m.group(1), finding) if m.group(1) in legal else m.group(0),
        narrative,
    )

    flags = [f"orphan_placeholder:{{{name}}}" for name in orphans] + \
            [f"stray_numeral:{s}" for s in strays]
    return ValidationResult(filled=filled, ok=not orphans and not strays,
                            flags=flags, orphans=orphans, strays=strays)


def validate_findings(findings: list[dict]) -> list[dict]:
    """Validate every finding's narrative. Returns new dicts: ``narrative`` is kept raw (auditable),
    ``narrative_filled`` / ``validated`` / ``validation_flags`` are populated."""
    out: list[dict] = []
    flagged = 0
    for finding in findings:
        res = validate_narrative(finding.get("narrative", "") or "", finding)
        flagged += 0 if res.ok else 1
        out.append({**finding, "narrative_filled": res.filled,
                    "validated": res.ok, "validation_flags": res.flags})
    logger.info("narrative_validator: %d/%d narratives valid (%d flagged)",
                len(out) - flagged, len(out), flagged)
    return out


# ===========================================================================
# 3. CLI — generate → validate → markdown report (the demonstrable artifact)
# ===========================================================================
def _finding_md(f: dict) -> str:
    status = "✓ valid" if f["validated"] else "✗ FLAGGED"
    flags = ("\n\n**Flags:** " + ", ".join(f["validation_flags"])) if f["validation_flags"] else ""
    quoted = "\n".join("> " + line for line in f["narrative_filled"].splitlines())
    return (f"### {f['finding_id']} · {status} · {f['alert_type']} · "
            f"{f['entity']}/{f['region']}/{f['segment']}{flags}\n\n"
            f"**Raw (placeholder-prose):**\n\n```\n{f['narrative']}\n```\n\n"
            f"**Filled:**\n\n{quoted}\n")


def _render_markdown(runs, *, audience, model, period) -> str:
    parts = [f"# Athena narrative validation — {period}", f"\n- **Model:** {model} · **Audience:** {audience}\n"]
    for numbers, style, out in runs:
        n_ok = sum(1 for f in out if f["validated"])
        parts.append(f"\n---\n\n## numbers = {numbers} · style = {style}  ({n_ok}/{len(out)} valid)\n")
        parts.extend(_finding_md(f) for f in out)
    return "\n".join(parts)


def main() -> int:
    import os

    from app.analytics.variance_engine import run_pipeline
    from app.llm.llm_client import call_llm
    from app.llm.narrative_generator import generate_narratives

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Generate narratives, then validate the no-number contract.")
    ap.add_argument("--numbers", choices=["withhold", "show"], default="withhold")
    ap.add_argument("--style", choices=["paragraph", "bullets"], default="paragraph")
    ap.add_argument("--audience", choices=["exec", "analyst"], default="exec")
    ap.add_argument("--limit", type=int, default=3, help="how many top-ranked findings to process")
    ap.add_argument("--compare", action="store_true", help="A/B numbers×style on the top --limit findings")
    ap.add_argument("--out", nargs="?", const="AUTO", default=None,
                    help="write a markdown report instead of the terminal; bare --out picks a default")
    args = ap.parse_args()

    result = run_pipeline()
    period = result.get("current_period", "")
    findings = result["findings"][: args.limit]
    combos = ([(n, s) for n in ("withhold", "show") for s in ("paragraph", "bullets")]
              if args.compare else [(args.numbers, args.style)])
    print(f"(generating + validating {len(findings)} finding(s) × {len(combos)} variant(s))")

    runs = []
    for numbers, style in combos:
        generated = generate_narratives(findings, client=call_llm, numbers=numbers, style=style,
                                        audience=args.audience)
        runs.append((numbers, style, validate_findings(generated)))

    if args.out is None:
        for numbers, style, out in runs:
            print(f"\n#### numbers={numbers} style={style} ####")
            for f in out:
                print(f"\n— {f['finding_id']} {'✓' if f['validated'] else '✗ ' + str(f['validation_flags'])} "
                      f"{f['alert_type']} —\n{f['narrative_filled']}")
    else:
        model = os.getenv("LLM_MODEL", "model")
        if args.out == "AUTO":
            safe = model.replace(":", "-").replace("/", "-")
            path = REPO_ROOT / "outputs" / f"validation_{safe}_{period}.md"
        else:
            path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(runs, audience=args.audience, model=model, period=period))
        total = sum(len(o) for _, _, o in runs)
        valid = sum(1 for _, _, o in runs for f in o if f["validated"])
        print(f"wrote {total} narrative(s) ({valid} valid) across {len(runs)} variant(s) to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
