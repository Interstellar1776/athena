"""gen_notes.py — produces operational_notes.csv (qualitative context).

Free-text operational commentary that feeds the retrieval/context layer — this
is what lets the narrative reference a likely cause. Notes are deterministic
(authored, not random), each scoped to a valid entity/segment or the literal
"ALL". The set deliberately mixes signal with benign noise so the Phase-7
retrieval-vs-filtering question has something real to test against.

Key seeded notes:
  • A ~May-7 campaign/bid-increase note on the hero channel — the cause the
    May-22 narrative connects the CPA spike to.
  • A ~May-19 note flagging the late April invoice (explains the accrual).
  • A ~May-9 note on the fallout channel (explains the conversion slip).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import shared

# (date, entity, region, segment, note_text, author) — "ALL" is the wildcard scope.
_NOTES = [
    # --- benign history noise ---
    (dt.date(2024, 4, 15), "ALL", "ALL", "ALL",
     "Annual compliance training scheduled across all channels for Q2. No operational impact expected.",
     "People Ops"),

    # --- hero CPA-spike cause (the note the May-22 narrative links to) ---
    (dt.date(2024, 5, 7), "ERCOT", "North", "Paid Search",
     "Launched an aggressive paid-search bid increase and a new acquisition campaign on May 6 to chase "
     "Q2 growth targets. Expect elevated cost-per-acquisition in the near term until creative and "
     "bids optimize.",
     "J. Rivera, Channel Marketing"),

    # --- benign note near the spike (retrieval should NOT prefer this) ---
    (dt.date(2024, 5, 3), "PJM", "East", "Broker",
     "Broker partner renewed annual agreement; commercial terms unchanged from last year.",
     "Partnerships"),

    # --- fallout cause ---
    (dt.date(2024, 5, 9), "ERCOT", "South", "Door_to_Door",
     "Two field crews down this week due to seasonal turnover. Lead quality and close rates slipping; "
     "more drop-offs than usual until we backfill.",
     "Field Operations"),

    # --- late-invoice / accrual context ---
    (dt.date(2024, 5, 19), "ERCOT", "North", "Paid Search",
     "Finance flagged a late April paid-search vendor invoice (~$9.8k overage) that will post in May. "
     "April CPA may be restated once it lands.",
     "Finance Operations"),

    # --- new-region launch context (first-run series) ---
    (dt.date(2024, 5, 16), "ERCOT", "West", "Broker",
     "ERCOT West territory officially launched. First broker deals expected this week; no historical "
     "baseline yet, so early metrics run off plan.",
     "Market Expansion"),
]


def generate(config) -> pd.DataFrame:
    """Return the operational-notes dataframe (authored, deterministic)."""
    rows = [
        {
            "date": d.isoformat(),
            "entity": entity,
            "region": region,
            "segment": segment,
            "note_text": note_text,
            "author": author,
        }
        for (d, entity, region, segment, note_text, author) in _NOTES
    ]
    return pd.DataFrame(rows)
