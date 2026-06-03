#!/usr/bin/env python3
"""build_fixtures.py — generate the committed clean + bad-data snapshots.

Each fixture is a full five-table mini-snapshot so the validator CLI can be pointed
straight at it. The ``clean/`` fixture is a small, self-consistent slice of the real
2024-05-22 snapshot; every ``bad_*`` fixture is that same clean slice with exactly
**one** rule deliberately broken, so a test can assert *which* check halts.

Run:  python tests/fixtures/build_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SNAPSHOT = REPO_ROOT / "data" / "snapshots" / "2024-05-22"
OUT_DIR = Path(__file__).resolve().parent / "snapshots"

FEEDS = ("sales", "conversions", "gl_actuals", "reference_data", "operational_notes")

# Which check each bad fixture is engineered to trip — the test contract.
EXPECTED_FAILURE = {
    "bad_missing_column": "schema:sales",
    "bad_bad_enum_segment": "content:sales",
    "bad_future_date": "content:sales",
    "bad_contract_term_violation": "content:sales",
    "bad_conversion_before_sale": "content:conversions",
    "bad_negative_amount": "content:gl_actuals",
    "bad_dup_customer_key": "keys_unique",
    "bad_orphan_conversion": "conversions_reference_sales",
    "bad_unresolved_gl_combo": "gl_combos_resolve",
    "bad_unknown_dim_tuple": "dim_tuples_known",
}


def _read(feed: str) -> pd.DataFrame:
    return pd.read_csv(SOURCE_SNAPSHOT / f"{feed}.csv", dtype=str, keep_default_na=False)


def _write(folder_name: str, frames: dict[str, pd.DataFrame]) -> None:
    folder = OUT_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    for feed in FEEDS:
        frames[feed].to_csv(folder / f"{feed}.csv", index=False)


def build_clean() -> dict[str, pd.DataFrame]:
    """A small, self-consistent slice of the real snapshot.

    sales is downsampled; conversions is filtered to the surviving submissions (so
    the referential and key contracts hold). gl_actuals / reference_data /
    operational_notes are kept whole — they're already small and keep the GL-tie-back
    and plan-coverage contracts intact."""
    sales = _read("sales").head(80).reset_index(drop=True)
    kept_keys = set(sales["customer_key"])
    conversions = _read("conversions")
    conversions = conversions[conversions["customer_key"].isin(kept_keys)].reset_index(drop=True)
    return {
        "sales": sales,
        "conversions": conversions,
        "gl_actuals": _read("gl_actuals"),
        "reference_data": _read("reference_data"),
        "operational_notes": _read("operational_notes"),
    }


def _copy(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {k: v.copy() for k, v in frames.items()}


def build_bad(clean: dict[str, pd.DataFrame]) -> dict[str, dict[str, pd.DataFrame]]:
    """One single-fault mutation per fixture name."""
    out: dict[str, dict[str, pd.DataFrame]] = {}

    # Schema: drop a required column.
    f = _copy(clean); f["sales"] = f["sales"].drop(columns=["segment"])
    out["bad_missing_column"] = f

    # Enum: an impossible acquisition channel.
    f = _copy(clean); f["sales"].loc[0, "segment"] = "Carrier_Pigeon"
    out["bad_bad_enum_segment"] = f

    # Type/temporal: a sale dated after the snapshot.
    f = _copy(clean); f["sales"].loc[0, "sale_date"] = "2099-01-01"
    out["bad_future_date"] = f

    # Cross-field: a Month_to_Month row carrying a contract term.
    f = _copy(clean)
    idx = f["sales"].index[f["sales"]["product_type"] == "Month_to_Month"][0]
    f["sales"].loc[idx, "contract_term_months"] = "24"
    out["bad_contract_term_violation"] = f

    # Within-row temporal: a gain landing before its submission.
    f = _copy(clean); f["conversions"].loc[0, "conversion_date"] = "2000-01-01"
    out["bad_conversion_before_sale"] = f

    # Numeric sanity: negative spend.
    f = _copy(clean); f["gl_actuals"].loc[0, "amount"] = "-500.00"
    out["bad_negative_amount"] = f

    # Uniqueness: a duplicated submission key.
    f = _copy(clean)
    f["sales"].loc[1, "customer_key"] = f["sales"].loc[0, "customer_key"]
    out["bad_dup_customer_key"] = f

    # Referential: a gain with no matching submission.
    f = _copy(clean); f["conversions"].loc[0, "customer_key"] = "888888888"
    out["bad_orphan_conversion"] = f

    # GL join: a ledger combo that gl_mapping can't resolve.
    f = _copy(clean); f["gl_actuals"].loc[0, "vendor"] = "GhostVendor Unmapped"
    out["bad_unresolved_gl_combo"] = f

    # Dimension roster: an invented service_territory → tuple not a known leaf.
    f = _copy(clean); f["sales"].loc[0, "service_territory"] = "BogusTDU"
    out["bad_unknown_dim_tuple"] = f

    return out


def main() -> int:
    if not SOURCE_SNAPSHOT.exists():
        raise SystemExit(f"source snapshot not found: {SOURCE_SNAPSHOT}")
    clean = build_clean()
    _write("clean", clean)
    bad = build_bad(clean)
    assert set(bad) == set(EXPECTED_FAILURE), "fixture set drifted from EXPECTED_FAILURE"
    for name, frames in bad.items():
        _write(name, frames)
    print(f"Wrote clean/ + {len(bad)} bad_* fixtures to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
