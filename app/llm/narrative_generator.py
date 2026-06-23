#!/usr/bin/env python3
"""narrative_generator.py — findings → placeholder-prose (the LLM never types a number).

Build Sequence §19 step 4 (context doc §3/§4/§18). This module turns each §14 finding into business
language by asking the LLM to write prose that references numbers **only** as named placeholders
(``{variance_pct}``), which Python fills later. It is the *generate* half of the spine; the *fill +
contract enforcement* is stage 5's ``narrative_validator``, and retrieval into ``retrieved_context``
is step 7 (the slot is read here from day one, empty for now).

Design (decisions in ``docs/decisions_log.md`` BS4; pitch in ``docs/the_hallucination_guard.md``):

* **The LLM owns the sentence; Python owns the numbers.** The only hard rule is "never type a digit —
  use the placeholders offered." Structure, cause, recommendation, tone stay free.
* **Three prompt knobs**, all settled empirically on a local model (built switchable, not hardcoded):
  ``numbers`` (withhold | show), ``style`` (paragraph | bullets), ``audience`` (exec | analyst).
* **Content (thin build):** explain + recommend; keep cause hedged (no real context yet — §ix causal
  honesty); acknowledge estimated values.
* **Per-finding call** (small feed; easy to ground/debug); a batched call is a later option.

CLI:
    LLM_PROVIDER=ollama LLM_MODEL=qwen3:32b python -m app.llm.narrative_generator --compare
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Callable

from app.llm.placeholders import available_placeholders, render_placeholder, PLACEHOLDERS

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SYSTEM_CONFIG = REPO_ROOT / "config" / "system_config.yaml"

# Defaults for the three knobs — provisional starting points; the right values are decided after the
# local-model A/B (see the verification plan), not asserted here.
DEFAULT_NUMBERS = "withhold"
DEFAULT_STYLE = "paragraph"
DEFAULT_AUDIENCE = "exec"

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# Prompt fragments for the two switchable axes.
_AUDIENCE_VOICE = {
    "exec": "Write for an operational leader: plain business language, decisive, minimal jargon.",
    "analyst": "Write for an analyst: you may reference method labels and added nuance.",
}
_STYLE_FORMAT = {
    "paragraph": "Write 2-3 sentences as a single short paragraph.",
    "bullets": "Write 3-4 short bullet points (what is happening / why it matters / recommended action).",
}


# ===========================================================================
# 1. Prompt construction
# ===========================================================================
def build_prompt(finding: dict, *, numbers: str = DEFAULT_NUMBERS, style: str = DEFAULT_STYLE,
                 audience: str = DEFAULT_AUDIENCE) -> tuple[str, str]:
    """Build the (system, user) prompt for one finding.

    System = the standing rules (the absolute no-digits rule + what to do/avoid + voice + format).
    User = this finding's qualitative facts, the legal placeholder menu, the (currently empty)
    retrieved-context slot, and — only when ``numbers="show"`` — each placeholder's rendered value.
    """
    if numbers not in ("withhold", "show"):
        raise ValueError(f"numbers must be 'withhold' or 'show', got {numbers!r}")

    system = (
        "You are an operations analyst writing a short alert about ONE finding from an analytics system.\n\n"
        "ABSOLUTE RULE — NUMBERS: Never write a number as digits. Every number you mention must be one "
        "of the placeholder tokens provided, written EXACTLY as given (e.g. {variance_pct}). Do not "
        "compute, round, or paraphrase any number in words (no 'a fifth', 'nearly double', 'about "
        "half'). Python replaces each placeholder with the exact value afterward.\n\n"
        "WHAT TO DO: Explain plainly what is happening and why it matters, and recommend one concrete "
        "next step. If the value is marked estimated, acknowledge the uncertainty in words. You may "
        "suggest a likely cause, but keep it tentative unless operational context is provided.\n\n"
        "DO NOT: invent metrics, decide the severity yourself, or state a cause as proven fact.\n\n"
        f"VOICE: {_AUDIENCE_VOICE.get(audience, _AUDIENCE_VOICE[DEFAULT_AUDIENCE])}\n"
        f"FORMAT: {_STYLE_FORMAT.get(style, _STYLE_FORMAT[DEFAULT_STYLE])}"
    )

    names = available_placeholders(finding)
    menu_lines = []
    for name in names:
        label = PLACEHOLDERS[name].label
        line = f"  {{{name}}} — {label}"
        if numbers == "show":
            line += f"  (value: {render_placeholder(name, finding)})"
        menu_lines.append(line)
    menu = "\n".join(menu_lines) if menu_lines else "  (none)"

    context = finding.get("retrieved_context") or "(none provided)"
    user = (
        "FINDING\n"
        f"- Metric: {finding.get('metric')}\n"
        f"- Where: {finding.get('segment')} in {finding.get('entity')} {finding.get('region')}\n"
        f"- Alert type: {finding.get('alert_type')}\n"
        f"- Severity: {finding.get('risk_level')}; direction: {finding.get('variance_direction')}; "
        f"confidence: {finding.get('confidence')}; estimated: {finding.get('estimated')}\n\n"
        "PLACEHOLDERS YOU MAY USE (use only these; write them exactly):\n"
        f"{menu}\n\n"
        f"OPERATIONAL CONTEXT: {context}\n\n"
        "Write the alert now."
    )
    return system, user


# ===========================================================================
# 2. Generation — one call per finding, set finding["narrative"]
# ===========================================================================
def generate_narratives(findings: list[dict], *, client: Callable[..., str],
                        numbers: str = DEFAULT_NUMBERS, style: str = DEFAULT_STYLE,
                        audience: str = DEFAULT_AUDIENCE, model: str | None = None) -> list[dict]:
    """Generate placeholder-prose for each finding. Pure over the input (returns new dicts).

    ``client`` is any ``call_llm``-shaped callable ``(system, user, *, model) -> str`` — injected so
    tests can stub it without a network/SDK.
    """
    out: list[dict] = []
    for finding in findings:
        system, user = build_prompt(finding, numbers=numbers, style=style, audience=audience)
        narrative = client(system, user, model=model)
        out.append({**finding, "narrative": narrative})
    logger.info("narrative_generator: generated %d narratives (numbers=%s style=%s audience=%s)",
                len(out), numbers, style, audience)
    return out


def generate(config_path: Path = DEFAULT_SYSTEM_CONFIG, *, numbers: str = DEFAULT_NUMBERS,
             style: str = DEFAULT_STYLE, audience: str = DEFAULT_AUDIENCE,
             limit: int | None = None) -> list[dict]:
    """End-to-end wrapper: load findings via the analytics pipeline, call the configured LLM.

    ``limit`` caps how many (top-ranked) findings are sent to the LLM — each finding is one call, so
    this is the knob that controls cost/latency when you only want to eyeball a few.
    """
    from app.analytics.variance_engine import run_pipeline
    from app.llm.llm_client import call_llm

    findings = run_pipeline(config_path)["findings"]
    if limit is not None:
        findings = findings[:limit]
    return generate_narratives(findings, client=call_llm, numbers=numbers, style=style, audience=audience)


# ===========================================================================
# 3. Dev preview-fill — read the grounded result (stage-4 verification only)
# ===========================================================================
def _preview_fill(narrative: str, finding: dict) -> str:
    """Substitute placeholders with their formatted values so a human can read the grounded prose.

    Scaffolding for eyeballing stage-4 output ONLY. The authoritative fill + contract enforcement
    (orphan placeholder / stray numeral) is stage 5's ``narrative_validator``. Unknown placeholders are
    left intact so they're visible as the orphans they are.
    """
    legal = set(available_placeholders(finding))

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        return render_placeholder(name, finding) if name in legal else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, narrative)


# ===========================================================================
# 4. CLI — run the generator for the configured snapshot; optional knob A/B
# ===========================================================================
def _print_findings(findings: list[dict], *, numbers: str, style: str, audience: str) -> None:
    """Generate (one LLM call per finding) and print RAW placeholder-prose + the filled PREVIEW."""
    from app.llm.llm_client import call_llm

    out = generate_narratives(findings, client=call_llm, numbers=numbers, style=style, audience=audience)
    print(f"\n#### numbers={numbers} style={style} audience={audience} ({len(out)} findings) ####")
    for f in out:
        print(f"\n— {f['finding_id']} {f['risk_level']} {f['alert_type']} "
              f"{f['entity']}/{f['region']}/{f['segment']} —")
        print("RAW (placeholder-prose):\n" + f["narrative"])
        print("PREVIEW (filled):\n" + _preview_fill(f["narrative"], f))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Generate placeholder-prose narratives for the feed.")
    ap.add_argument("--numbers", choices=["withhold", "show"], default=DEFAULT_NUMBERS)
    ap.add_argument("--style", choices=["paragraph", "bullets"], default=DEFAULT_STYLE)
    ap.add_argument("--audience", choices=["exec", "analyst"], default=DEFAULT_AUDIENCE)
    ap.add_argument("--limit", type=int, default=3,
                    help="how many top-ranked findings to generate (each is one LLM call)")
    ap.add_argument("--compare", action="store_true",
                    help="A/B the knobs: numbers×style (audience fixed) on the top --limit findings")
    args = ap.parse_args()

    # Load + rank findings ONCE, then generate only the few we'll print (each finding = one LLM call).
    from app.analytics.variance_engine import run_pipeline

    findings = run_pipeline()["findings"][: args.limit]
    combos = ([(n, s) for n in ("withhold", "show") for s in ("paragraph", "bullets")]
              if args.compare else [(args.numbers, args.style)])
    print(f"(generating {len(findings)} finding(s) × {len(combos)} variant(s) "
          f"= {len(findings) * len(combos)} LLM call(s))")
    for numbers, style in combos:
        _print_findings(findings, numbers=numbers, style=style, audience=args.audience)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
