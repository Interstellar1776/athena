"""gen_transcripts.py — synthetic raw meeting transcripts (the pre-extraction source).

`operational_notes.csv` (from `gen_notes.py`) is a clean, perfectly-tagged **stand-in** for what a real
pipeline would distill out of messy meeting transcripts. This module produces those messy transcripts:
free-form, multi-speaker, **untagged** dialogue that embeds the same operational points `gen_notes`
authored — so `note_extractor` (Build Sequence §19 step 8) has a realistic hard path to reconstruct,
and the RAG-vs-filtering bake-off has genuine untagged input to run against.

Design:
  • **Ground truth = the `gen_notes` rows.** Each transcript buries the same points (the May-6/7
    door-to-door commission push, the May-19 late-April `~$9.8k` invoice, the May-9 telemarketing
    turnover, the May-16 ERCOT West launch) plus benign noise, but with **no tags** — the channel and
    geography are implied in conversation, for the LLM to infer.
  • **Numbers appear verbatim** (`~$9.8k`) so the extractor's provenance check (numbers must trace to
    the transcript) passes and the figure stays traceable context, never a metric (§4).
  • **Deterministic / authored**, not random — a stable fixture the extractor is tested against.

CLI:
    python -m scripts.generators.gen_transcripts            # write *.txt under data/contextual/transcripts/
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "contextual" / "transcripts"


# (stem, transcript_text) — each is one saved meeting. Points are untagged; geography/channel is implied.
_TRANSCRIPTS = [
    ("2024-05-07_weekly_ops_sync", """\
Weekly Operations Sync — Tuesday, May 7, 2024
Attendees: J. Rivera (Channel Marketing), P. Osei (Finance), L. Zhang (Contact Center)

Rivera: Quick one from the field side. Yesterday, May 6, we pushed the door-to-door team in ERCOT
North pretty hard — we're chasing the Q2 growth target and we were behind. We bumped commissions,
added crews and extra hours, and stood up a short-term sales-incentive bonus to get people moving.
Osei: Understood. That's going to show up in cost per acquisition though.
Rivera: Yeah, expect elevated CPA in the near term for that channel. The incentive payouts mostly land
at month-end, so it'll get worse before the numbers settle.
Osei: Noted, I'll keep an eye on the North door-to-door spend.
Zhang: Nothing major from the call centers this week, I'll bring numbers next time.
Osei: Good. One housekeeping item — the annual compliance training is scheduled across all channels
for Q2, but there's no operational impact expected, so ignore it for planning.
"""),
    ("2024-05-09_contact_center_standup", """\
Contact Center Ops Standup — Thursday, May 9, 2024
Attendees: L. Zhang (Contact Center Operations), M. Boone (Workforce)

Zhang: The outbound calling center down in ERCOT South is having a rough week. We've had high agent
turnover — a bunch of experienced reps left and we're running with new people.
Boone: How's it hitting the funnel?
Zhang: Lead quality and close rates are both slipping. Honestly we're seeing more drop-offs than usual
on the telemarketing side. It'll stay soft until we rehire and retrain.
Boone: Okay, I'll flag it so nobody mistakes it for a demand problem. It's a staffing issue.
"""),
    ("2024-05-19_finance_review", """\
Finance Review — Sunday, May 19, 2024
Attendees: P. Osei (Finance Operations), J. Rivera (Channel Marketing)

Osei: Heads up on an accrual. We found a late April field-sales commission invoice that didn't post in
time — it's a ~$9.8k overage and it's going to land in May.
Rivera: For the North door-to-door team?
Osei: Right, ERCOT North door-to-door. Because it's an April cost hitting now, April CPA for that
channel may get restated once it posts.
Rivera: Makes sense, that lines up with the commission push we ran.
Osei: I'll footnote it so the restatement isn't a surprise.
"""),
    ("2024-05-16_expansion_review", """\
Market Expansion Review — Thursday, May 16, 2024
Attendees: D. Fields (Market Expansion), P. Osei (Finance)

Fields: The ERCOT West territory officially launched. We expect the first telemarketing deals in West
this week.
Osei: Do we have a baseline to measure against?
Fields: Not yet — there's no history for West, so early metrics are going to run off plan until we
accumulate some actuals. Don't read too much into the first couple weeks.
Osei: Understood, I'll label anything from West as first-run.
Fields: One more, unrelated — the direct-mail print vendor over in PJM East renewed their annual
agreement back on May 3. Commercial terms are unchanged from last year, so no cost impact.
"""),
]


def generate(config=None) -> dict[str, str]:
    """Return ``{stem: transcript_text}`` for every synthetic meeting (authored, deterministic)."""
    return dict(_TRANSCRIPTS)


def write(out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write each transcript to ``out_dir/<stem>.txt`` and return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for stem, text in generate().items():
        path = out_dir / f"{stem}.txt"
        path.write_text(text)
        paths.append(path)
    return paths


def main() -> int:
    paths = write()
    print(f"wrote {len(paths)} transcript(s) to {DEFAULT_OUT_DIR}:")
    for p in paths:
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
