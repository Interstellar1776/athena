#!/usr/bin/env python3
"""generate_snapshots.py — orchestrator for Athena's synthetic data foundation.

Establishes the shared config + seed, calls each single-responsibility generator,
VALIDATES cross-table join integrity (and halts loudly on any violation — bad
data must never flow downstream), then assembles the four cumulative snapshots
that walk the demo narrative arc (context doc §8).

Each snapshot is a cumulative cut of the full timeline: all rows dated on/before
the snapshot date (GL filtered by posting_date, which is what keeps the late
April invoice hidden until the May-22 snapshot).

Usage:
    python scripts/generate_snapshots.py                  # default knobs, CSV output
    python scripts/generate_snapshots.py --format both    # write CSV and XLSX
    python scripts/generate_snapshots.py --cpa-spike-magnitude 0.30 --no-late-invoice
    python scripts/generate_snapshots.py --history-months 6
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

# --- make the generators package importable, then import it ---
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from generators import (  # noqa: E402
    shared, gen_sales, gen_conversions, gen_gl, gen_reference, gen_notes,
)

CONFIG_DIR = REPO_ROOT / "config"
SNAPSHOTS_DIR = REPO_ROOT / "data" / "snapshots"

TABLES = ("sales", "conversions", "gl_actuals", "reference_data", "operational_notes")


class PipelineHalt(Exception):
    """Raised to halt the pipeline loudly with a plain-language reason."""


# ---------------------------------------------------------------------------
# Validation — runs BEFORE any write. Any failure halts the pipeline loudly.
# ---------------------------------------------------------------------------
def _dim_keys(df) -> list:
    """Normalize each row's dimension columns to the canonical key tuple."""
    return [shared.normalize_dim_tuple(t)
            for t in df[shared.DIMENSION_COLUMNS].itertuples(index=False, name=None)]


def validate(sales, conversions, gl, reference, notes, gl_mapping) -> None:
    problems: list[str] = []

    valid_keys = {s.key for s in shared.SERIES}
    valid_note_scopes = {(s.entity, s.region, s.segment) for s in shared.SERIES}

    # 1) No invented dimension tuples in sales / conversions / reference.
    for name, df in (("sales", sales), ("conversions", conversions), ("reference_data", reference)):
        bad = set(_dim_keys(df)) - valid_keys
        if bad:
            problems.append(f"{name}: {len(bad)} unknown dimension tuple(s): {sorted(map(str, bad))[:3]}")

    # 2) Notes reference a real (entity, region, segment) scope, or use 'ALL' as a
    #    wildcard on any level.
    for entity, region, segment in notes[["entity", "region", "segment"]].itertuples(index=False, name=None):
        if "ALL" in (entity, region, segment):
            continue
        if (entity, region, segment) not in valid_note_scopes:
            problems.append(f"operational_notes: unknown scope ({entity}, {region}, {segment})")

    # 3) gl_mapping.csv must equal the canonical mapping derived from shared.SERIES
    #    (now keyed by cost_center, gl_account, entity, region, segment, spend_category).
    cols = ["cost_center", "gl_account", "entity", "region", "segment", "spend_category"]
    canon = {tuple(r[c] for c in cols) for r in shared.canonical_gl_mapping_rows()}
    authored = {tuple(r) for r in gl_mapping[cols].itertuples(index=False, name=None)}
    if canon != authored:
        missing, extra = canon - authored, authored - canon
        if missing:
            problems.append(f"gl_mapping.csv missing rows vs shared.SERIES: {sorted(missing)[:5]}")
        if extra:
            problems.append(f"gl_mapping.csv has rows not in shared.SERIES: {sorted(extra)[:5]}")

    # 4) Every GL cost_center maps via gl_mapping to a real series.
    mapped_centers = set(gl_mapping["cost_center"])
    for cc in set(gl["cost_center"]):
        if cc not in mapped_centers:
            problems.append(f"gl_actuals: cost_center '{cc}' not in gl_mapping.csv")
        elif shared.series_by_cost_center(cc) is None:
            problems.append(f"gl_actuals: cost_center '{cc}' maps to no series in shared.SERIES")

    # 5) Every series has a plan row covering the active period (fallback target).
    plan = reference[reference["reference_type"] == "plan"]
    plan_keys_active = set(_dim_keys(plan[plan["date"].str.startswith(shared.ACTIVE_PERIOD)]))
    for s in shared.SERIES:
        if s.key not in plan_keys_active:
            problems.append(f"reference_data: no active-period plan row for series {s.key}")

    # 6) Sales/conversions integrity — gains reconcile back to submissions on
    #    customer_key. Fallout is the unmatched complement (not stored), so the
    #    only contract here is referential: every conversion has a submission.
    if sales["customer_key"].duplicated().any():
        problems.append("sales: duplicate customer_key")
    if conversions["customer_key"].duplicated().any():
        problems.append("conversions: duplicate customer_key")

    orphans = set(conversions["customer_key"]) - set(sales["customer_key"])
    if orphans:
        problems.append(f"conversions: {len(orphans)} customer_key(s) with no matching submission in sales")
    if (conversions["conversion_date"] < conversions["sale_date"]).any():
        problems.append("conversions: conversion_date precedes sale_date")

    # 7) Numeric sanity.
    if (gl["amount"] < 0).any():
        problems.append("gl_actuals: negative amount")
    price = conversions["price_per_unit"].dropna()
    if len(price) and (price < 0).any():
        problems.append("conversions: negative price_per_unit")

    if problems:
        raise PipelineHalt(
            "Join/integrity validation failed — refusing to write snapshots:\n  - "
            + "\n  - ".join(problems)
        )


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------
def cut_snapshot(sales, conversions, gl, reference, notes, snap_date: dt.date) -> dict:
    """Cumulative cut: all rows dated on/before snap_date. Each feed is cut on the
    date the data lands — sales by sale_date, conversions by conversion_date (so a
    late-landing gain appears only from its conversion date), GL by posting_date,
    reference/notes by date."""
    iso = snap_date.isoformat()
    return {
        "sales": sales[sales["sale_date"] <= iso].reset_index(drop=True),
        "conversions": conversions[conversions["conversion_date"] <= iso].reset_index(drop=True),
        "gl_actuals": gl[gl["posting_date"] <= iso].reset_index(drop=True),
        "reference_data": reference[reference["date"] <= iso].reset_index(drop=True),
        "operational_notes": notes[notes["date"] <= iso].reset_index(drop=True),
    }


def write_table(df: pd.DataFrame, folder: Path, name: str, fmt: str) -> None:
    if fmt in ("csv", "both"):
        df.to_csv(folder / f"{name}.csv", index=False)
    if fmt in ("xlsx", "both"):
        # one workbook per table mirrors the per-table CSV layout the loader expects
        df.to_excel(folder / f"{name}.xlsx", index=False)


# ---------------------------------------------------------------------------
# Inspection summary — the realism gate (Build Sequence step 1).
# ---------------------------------------------------------------------------
def _series_rows(df, s):
    """Rows of df whose dimension tuple equals series s's key."""
    return df[[k == s.key for k in _dim_keys(df)]]


def print_summary(snapshots: dict, config) -> None:
    hero = next(s for s in shared.SERIES if s.role == "hero")
    fallout = next(s for s in shared.SERIES if s.role == "fallout")
    new = next(s for s in shared.SERIES if s.role == "new")

    print("\n" + "=" * 78)
    print("SNAPSHOT SUMMARY — walk the arc (calm -> drift -> building -> confirmed HIGH -> final)")
    print("=" * 78)
    for snap_date, tables in snapshots.items():
        s_df, c_df, g, r, n = (tables["sales"], tables["conversions"], tables["gl_actuals"],
                               tables["reference_data"], tables["operational_notes"])

        # Hero active-period CPA = May acquisition spend (by document_date) / May
        # conversions present in the cut (by conversion_date — so the open period
        # shows the gains that have actually landed).
        g_acq_may = g[(g["gl_account"] == shared.GL_ACCOUNT_ACQUISITION)
                      & (g["cost_center"] == hero.cost_center)
                      & (g["document_date"].str.startswith(shared.ACTIVE_PERIOD))]
        c_hero_may = _series_rows(c_df, hero)
        c_hero_may = c_hero_may[c_hero_may["conversion_date"].str.startswith(shared.ACTIVE_PERIOD)]
        conv = len(c_hero_may)
        hero_cpa = (g_acq_may["amount"].sum() / conv) if conv else float("nan")
        hero_var = (hero_cpa / hero.base_cpa - 1.0) * 100 if conv else float("nan")

        # Fallout (trailing matured window), derived by anti-join. We look at the
        # fallout series' submissions in the 7 days ending at the maturity cutoff
        # (sale_date <= snap - max lag) — old enough that their gains have landed —
        # and take the share with no matching conversion. A trailing window (vs.
        # cumulative MTD) reflects the *current* fallout rate, so the ramp shows.
        cutoff = dt.date.fromisoformat(snap_date) - dt.timedelta(days=config.conv_lag_max_days)
        window_start = (cutoff - dt.timedelta(days=7)).isoformat()
        s_fo = _series_rows(s_df, fallout)
        s_fo = s_fo[(s_fo["sale_date"] > window_start) & (s_fo["sale_date"] <= cutoff.isoformat())]
        fo_total = len(s_fo)
        matched = s_fo["customer_key"].isin(set(c_df["customer_key"])).sum()
        fo_rate = ((fo_total - matched) / fo_total * 100) if fo_total else float("nan")

        # Late/accrued entries present (document month precedes posting month).
        late = g[g.apply(lambda x: x["document_date"][:7] < x["posting_date"][:7], axis=1)]
        n_forecast = int((r["reference_type"] == "forecast").sum())
        new_sales = len(_series_rows(s_df, new))

        print(f"\n[{snap_date}]  rows: sales={len(s_df)} conv={len(c_df)} gl={len(g)} "
              f"ref={len(r)} notes={len(n)}")
        print(f"   hero May CPA       : {hero_cpa:7.2f}  vs plan {hero.base_cpa:.2f}  "
              f"({hero_var:+.1f}%)  [{conv} conv landed]")
        print(f"   fallout (matured)  : {fo_rate:5.1f}%  (n={fo_total}; baseline ~{(1-fallout.base_conv_rate)*100:.0f}%, "
              f"daily target ~{config.fallout_target_rate*100:.0f}% by day {shared.LAST_INMONTH_SNAPSHOT.day})")
        print(f"   forecast rows      : {n_forecast:2d}   late/accrued GL rows: {len(late)}   "
              f"new-series ({new.region}) sales: {new_sales}")
        print(f"   notes visible      : {len(n)}")


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------
def build_config(args) -> shared.NarrativeConfig:
    cfg = shared.NarrativeConfig()
    if args.cpa_spike_magnitude is not None:
        cfg.cpa_spike_magnitude = args.cpa_spike_magnitude
    if args.fallout_target_rate is not None:
        cfg.fallout_target_rate = args.fallout_target_rate
    if args.forecast_divergence is not None:
        cfg.forecast_divergence = args.forecast_divergence
    if args.noise_sd is not None:
        cfg.noise_sd = args.noise_sd
    if args.no_late_invoice:
        cfg.late_invoice_enabled = False
    return cfg


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Generate Athena demo snapshots.")
    p.add_argument("--format", choices=["csv", "xlsx", "both"], default="csv",
                   help="output format for snapshot tables (default: csv)")
    p.add_argument("--history-months", type=int, default=None,
                   help=f"override trailing-history depth (default: {shared.HISTORY_MONTHS})")
    p.add_argument("--cpa-spike-magnitude", type=float, default=None,
                   help=f"hero spend-side cumulative CPA-vs-plan uplift by month end "
                        f"(default: {shared.NarrativeConfig.cpa_spike_magnitude})")
    p.add_argument("--fallout-target-rate", type=float, default=None,
                   help=f"fallout series daily fell_out/submissions rate by month end "
                        f"(default: {shared.NarrativeConfig.fallout_target_rate})")
    p.add_argument("--forecast-divergence", type=float, default=None,
                   help="forecast CPA divergence from plan (default: 0.12)")
    p.add_argument("--noise-sd", type=float, default=None,
                   help="global multiplicative noise stddev (default: 0.04)")
    p.add_argument("--no-late-invoice", action="store_true",
                   help="disable the engineered late/accrued GL entry")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # History-depth selector: recompute the global history start before generating.
    if args.history_months is not None:
        shared.HISTORY_MONTHS = args.history_months
        shared.HISTORY_START = (shared.ACTIVE_PERIOD_START
                                - dt.timedelta(days=args.history_months * 30)).replace(day=1)

    config = build_config(args)

    print("Athena — generating synthetic snapshots")
    print(f"  seed={shared.RANDOM_SEED}  history_months={shared.HISTORY_MONTHS}  "
          f"history_start={shared.HISTORY_START}  format={args.format}")
    print(f"  series={len(shared.SERIES)}  snapshots={[d.isoformat() for d in shared.SNAPSHOT_DATES]}")

    # --- generate full-timeline tables ---
    # gen_sales produces the submissions feed (no outcome); gen_conversions decides
    # which submissions convert and emits the gains (sharing customer_key, so
    # fallout is the unmatched complement).
    sales = gen_sales.generate(config)
    conversions = gen_conversions.generate(config, sales)
    gl = gen_gl.generate(config)
    reference = gen_reference.generate(config)
    notes = gen_notes.generate(config)

    # --- load static config + validate (halt loudly on any problem) ---
    gl_mapping = pd.read_csv(CONFIG_DIR / "gl_mapping.csv", dtype={"gl_account": str})
    try:
        validate(sales, conversions, gl, reference, notes, gl_mapping)
    except PipelineHalt as e:
        print(f"\nPIPELINE HALTED\n{e}", file=sys.stderr)
        return 1

    # --- assemble + write cumulative snapshots ---
    snapshots = {}
    for snap_date in shared.SNAPSHOT_DATES:
        tables = cut_snapshot(sales, conversions, gl, reference, notes, snap_date)
        snapshots[snap_date.isoformat()] = tables
        folder = SNAPSHOTS_DIR / snap_date.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        for name in TABLES:
            write_table(tables[name], folder, name, args.format)

    print_summary(snapshots, config)
    print(f"\nWrote {len(shared.SNAPSHOT_DATES)} snapshots to {SNAPSHOTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
