"""Tests for the narrative generator (Build Sequence §19 step 4).

Pins the prompt contract and the generation loop with a STUB client (no network, no SDK): the prompt
carries the finding facts, the per-finding legal placeholder menu, and the retrieved-context seam; the
number-visibility knob shows/withholds raw values; the generator sets ``narrative`` on every finding;
and the dev preview-fill substitutes placeholders correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.llm import narrative_generator as ng  # noqa: E402

# Reuse the synthetic finding from the placeholder tests.
from tests.test_placeholders import cpa_finding  # noqa: E402


class StubClient:
    """A call_llm-shaped stub that records prompts and returns a fixed placeholder-prose string."""

    def __init__(self, reply: str = "CPA is {variance_pct} above plan; review commissions."):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, *, model=None) -> str:
        self.calls.append((system, user))
        return self.reply


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------
def test_prompt_carries_facts_menu_and_context_seam():
    system, user = ng.build_prompt(cpa_finding(), numbers="withhold")
    # The absolute no-digits rule lives in the system prompt.
    assert "Never write a number as digits" in system
    # Finding facts + the legal placeholder menu + the retrieved-context seam are in the user prompt.
    assert "cost_per_acquisition" in user and "Door_to_Door" in user
    assert "{variance_pct}" in user and "{actual}" in user
    assert "OPERATIONAL CONTEXT" in user
    # A placeholder the finding can't fill must not be offered.
    assert "{projected_linear}" not in user


def test_withhold_hides_values_show_reveals_them():
    _, user_withhold = ng.build_prompt(cpa_finding(), numbers="withhold")
    _, user_show = ng.build_prompt(cpa_finding(), numbers="show")
    assert "$160.74" not in user_withhold          # withhold: no raw values in the prompt
    assert "$160.74" in user_show                  # show: rendered values appear in the menu


def test_style_and_audience_change_the_system_prompt():
    sys_bullets, _ = ng.build_prompt(cpa_finding(), style="bullets")
    sys_para, _ = ng.build_prompt(cpa_finding(), style="paragraph")
    assert "bullet" in sys_bullets.lower()
    assert "paragraph" in sys_para.lower()
    sys_analyst, _ = ng.build_prompt(cpa_finding(), audience="analyst")
    assert "analyst" in sys_analyst.lower()


# ---------------------------------------------------------------------------
# generate_narratives
# ---------------------------------------------------------------------------
def test_generate_sets_narrative_on_every_finding_via_client():
    findings = [cpa_finding(), {**cpa_finding(), "finding_id": "F-002"}]
    stub = StubClient()
    out = ng.generate_narratives(findings, client=stub)
    assert len(out) == 2
    assert all(f["narrative"] == stub.reply for f in out)
    assert len(stub.calls) == 2
    # Input findings are not mutated (pure over input).
    assert findings[0]["narrative"] == ""


# ---------------------------------------------------------------------------
# _preview_fill
# ---------------------------------------------------------------------------
def test_preview_fill_substitutes_known_and_leaves_orphans():
    f = cpa_finding()
    text = "CPA is {variance_pct} over plan, total {total_spend}; {made_up} stays."
    filled = ng._preview_fill(text, f)
    assert "28.6%" in filled and "$13,984" in filled
    assert "{variance_pct}" not in filled and "{total_spend}" not in filled
    assert "{made_up}" in filled                     # orphan left visible (caught by stage 5)
