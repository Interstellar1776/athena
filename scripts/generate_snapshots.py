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

from generators import shared, gen_sales, gen_gl, gen_reference, gen_notes  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"
SNAPSHOTS_DIR = REPO_ROOT / "data" / "snapshots"

TABLES = ("actuals", "gl_actuals", "reference_data", "operational_notes")


class PipelineHalt(Exception):
    """Raised to halt the pipeline loudly with a plain-language reason."""


# ---------------------------------------------------------------------------
# Validation — runs BEFORE any write. Any failure halts the pipeline loudly.
# ---------------------------------------------------------------------------
def validate(actuals, gl, reference, notes, gl_mapping) -> None:
    problems: list[str] = []

    valid_keys = {s.key for s in shared.SERIES}
    valid_dims = {(s.entity, s.segment) for s in shared.SERIES}

    # 1) No invented dimensions in actuals / reference (full key incl product_type).
    for name, df in (("actuals", actuals), ("reference_data", reference)):
        keys = set(map(tuple, df[["entity", "segment", "product_type"]].fillna("").itertuples(index=False, name=None)))
        # product_type may legitimately be present; compare on (entity, segment, product_type)
        bad = {k for k in keys if (k[0], k[1], k[2]) not in valid_keys}
        if bad:
            problems.append(f"{name}: {len(bad)} row-group(s) reference unknown (entity,segment,product_type): {sorted(bad)[:5]}")

    # 2) Notes reference a real (entity, segment) or the literal 'ALL'.
    for entity, segment in notes[["entity", "segment"]].itertuples(index=False, name=None):
        if entity == "ALL" or segment == "ALL":
            continue
        if (entity, segment) not in valid_dims:
            problems.append(f"operational_notes: unknown (entity,segment) ({entity}, {segment})")

    # 3) gl_mapping.csv must equal the canonical mapping derived from shared.SERIES.
    canon = {(r["cost_center"], r["gl_account"], r["entity"], r["segment"], r["spend_category"])
             for r in shared.canonical_gl_mapping_rows()}
    authored = {tuple(r) for r in gl_mapping[["cost_center", "gl_account", "entity", "segment", "spend_category"]]
                .itertuples(index=False, name=None)}
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
    plan_keys_active = set(
        map(tuple, plan[plan["date"].str.startswith(shared.ACTIVE_PERIOD)][["entity", "segment", "product_type"]]
            .itertuples(index=False, name=None))
    )
    for s in shared.SERIES:
        if s.key not in plan_keys_active:
            problems.append(f"reference_data: no active-period plan row for series {s.key}")

    # 6) Table-wide invariants.
    bad_conv = actuals[actuals["volume_converted"] > actuals["volume_in"]]
    if len(bad_conv):
        problems.append(f"actuals: {len(bad_conv)} row(s) violate volume_converted <= volume_in")
    for name, df, cols in (
        ("actuals", actuals, ["volume_in", "volume_converted", "volume_lost"]),
        ("gl_actuals", gl, ["amount"]),
    ):
        for col in cols:
            if (df[col] < 0).any():
                problems.append(f"{name}: negative values in '{col}'")

    if problems:
        raise PipelineHalt(
            "Join/integrity validation failed — refusing to write snapshots:\n  - "
            + "\n  - ".join(problems)
        )


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------
def cut_snapshot(actuals, gl, reference, notes, snap_date: dt.date) -> dict:
    """Cumulative cut: all rows dated on/before snap_date (GL by posting_date)."""
    iso = snap_date.isoformat()
    return {
        "actuals": actuals[actuals["date"] <= iso].reset_index(drop=True),
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
def _hero_series():
    return next(s for s in shared.SERIES if s.role == "hero")


def print_summary(snapshots: dict, config) -> None:
    hero = _hero_series()
    fallout = next(s for s in shared.SERIES if s.role == "fallout")

    print("\n" + "=" * 78)
    print("SNAPSHOT SUMMARY — walk the arc (calm -> drift -> building -> confirmed HIGH)")
    print("=" * 78)
    for snap_date, tables in snapshots.items():
        a, g, r, n = (tables["actuals"], tables["gl_actuals"],
                      tables["reference_data"], tables["operational_notes"])

        # Hero May-period CPA = May acquisition spend (by document_date) / May conversions.
        g_acq_may = g[(g["gl_account"] == shared.GL_ACCOUNT_ACQUISITION)
                      & (g["cost_center"] == hero.cost_center)
                      & (g["document_date"].str.startswith(shared.ACTIVE_PERIOD))]
        a_hero_may = a[(a["entity"] == hero.entity) & (a["segment"] == hero.segment)
                       & (a["product_type"] == hero.product_type)
                       & (a["date"].str.startswith(shared.ACTIVE_PERIOD))]
        conv = a_hero_may["volume_converted"].sum()
        hero_cpa = (g_acq_may["amount"].sum() / conv) if conv else float("nan")
        hero_var = (hero_cpa / hero.base_cpa - 1.0) * 100 if conv else float("nan")

        # Fallout rate (active period to date) for the fallout series.
        a_fo = a[(a["entity"] == fallout.entity) & (a["segment"] == fallout.segment)
                 & (a["date"].str.startswith(shared.ACTIVE_PERIOD))]
        fo_in = a_fo["volume_in"].sum()
        fo_rate = (a_fo["volume_lost"].sum() / fo_in * 100) if fo_in else float("nan")

        # Late/accrued entries present (document month precedes posting month).
        late = g[g.apply(lambda x: x["document_date"][:7] < x["posting_date"][:7], axis=1)]
        n_forecast = int((r["reference_type"] == "forecast").sum())
        west_actuals = int(((a["entity"] == "ERCOT West")).sum())

        print(f"\n[{snap_date}]  rows: actuals={len(a)} gl={len(g)} ref={len(r)} notes={len(n)}")
        print(f"   hero May CPA       : {hero_cpa:7.2f}  vs plan {hero.base_cpa:.2f}  "
              f"({hero_var:+.1f}%)")
        print(f"   fallout rate (MTD) : {fo_rate:5.1f}%  (baseline ~{(1-fallout.base_conv_rate)*100:.0f}%, "
              f"daily target ~{config.fallout_target_rate*100:.0f}% by day {shared.SNAPSHOT_DATES[-1].day})")
        print(f"   forecast rows      : {n_forecast:2d}   late/accrued GL rows: {len(late)}   "
              f"ERCOT West actuals: {west_actuals}")
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
                   help="hero cumulative CPA-vs-plan by month end (default: 0.22)")
    p.add_argument("--fallout-target-rate", type=float, default=None,
                   help="fallout series volume_lost/volume_in by month end (default: 0.23)")
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
    actuals = gen_sales.generate(config)
    gl = gen_gl.generate(config)
    reference = gen_reference.generate(config)
    notes = gen_notes.generate(config)

    # --- load static config + validate (halt loudly on any problem) ---
    gl_mapping = pd.read_csv(CONFIG_DIR / "gl_mapping.csv", dtype={"gl_account": str})
    try:
        validate(actuals, gl, reference, notes, gl_mapping)
    except PipelineHalt as e:
        print(f"\nPIPELINE HALTED\n{e}", file=sys.stderr)
        return 1

    # --- assemble + write cumulative snapshots ---
    snapshots = {}
    for snap_date in shared.SNAPSHOT_DATES:
        tables = cut_snapshot(actuals, gl, reference, notes, snap_date)
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
