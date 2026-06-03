# energy_industry_notes

Authored **operational notes** for the retail-energy reference/demo industry — the
qualitative context layer that lets Athena's narrative reference a *likely cause*
for a flagged finding (e.g. the May-22 Door-to-Door CPA spike). These are
hand-written to read like a real field organization's running commentary, and are
grounded in the events the snapshot data already contains (the CPA spike, the
Telemarketing fallout, the late April commission invoice, the ERCOT West launch,
the month-end incentive spiffs, the post-close restatement).

## File

`operational_notes.csv` — schema (matches `docs/data_dictionary.md`):

| Field | Notes |
|-------|-------|
| `date` | ISO `YYYY-MM-DD`. |
| `entity` | Market, or `ALL`. |
| `region` | Region, or `ALL`. |
| `segment` | Acquisition channel, or `ALL`. |
| `note_text` | Free-text field commentary. Casual approximate figures are allowed (these are human-authored *input*, not model output, so the no-numeral rule for the narrative layer does not apply). |
| `author` | Note source (team / person). |

## Conventions

- **Scope.** `entity` / `region` / `segment` use the literal string `"ALL"` (not
  blank) as a per-level wildcard, so every note carries an explicit scope.
  Non-`ALL` scopes must match a real `(entity, region, segment)` channel×geography
  **unit** in the roster (`scripts/generators/shared.py:UNITS`) — there are 12.
- **No asserted metrics.** Notes describe what's happening in the field; they do
  not state precise metrics that Python owns (Python calculates every number).

## Pivoting to another industry

This folder is the demo industry's content. Pivoting to a different industry is
just a sibling folder of authored notes (e.g. `saas_industry_notes/`) using the
same schema and the `"ALL"` convention — no code changes required.
