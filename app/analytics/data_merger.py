#!/usr/bin/env python3
"""data_merger.py — join the cleaned feeds to their reference dimensions.

Build Sequence §19 step 3 (context doc §15), after ``data_loader``. It takes the
cleaned dataframes dict and produces the *joined, aggregated* frames the metrics layer
consumes. It computes **no business metrics** (CPA, fallout rate, margin) — it only
joins, aggregates to the right grain, and counts. Metrics live in ``metrics_calculator``.

Design (decided with the project owner — see commit history / plan):

* **Aggregate, then join 1:1 — never record-level.** ``reference_data`` is a *monthly*
  plan/forecast (one row per leaf per month); the operational feeds carry a full year of
  trailing history. Joining record-level on the dimensions alone would multiply every
  actual by 3–13 monthly plan rows. So actuals are rolled up to **leaf × period** and
  joined 1:1 to the plan for that leaf+period — the way a real variance pipeline works.

* **Period = calendar month** (``YYYY-MM``). Plan rows are dated first-of-month; forecast
  rows are dated to their *issue* date and treated as targeting their **issue month**
  (the schema has no explicit target-period column — documented assumption; it holds for
  this data, where forecasts are issued in the month they cover).

* **Conversions counted on both axes.** ~6.5% of gains land in a different month than the
  sale. ``volume_converted_landed`` (by ``conversion_date``) feeds CPA/volume-vs-plan;
  ``volume_converted_cohort`` (by ``sale_date`` month) feeds fallout. Both are emitted so
  downstream isn't forced into a lossy choice.

* **GL is resolved, not aggregated.** ``gl_mapping`` carries only entity/region/segment
  (the *unit* grain), so the merger attaches geography + ``spend_category`` to each GL
  line and filters overhead out of the acquisition view. Period bucketing and the
  completeness states (open/closed/restated/accrued) are ``gl_processor``'s job (next).

* **Forecast kept accessible.** Plan is the primary 1:1 join; forecast ref columns are
  attached in parallel (``*_ref_fc``) so the plan-vs-forecast-gap alert is possible.

* **Reference grain caveat (do not sum across leaves):** ``volume_*_ref`` and ``cost_ref``
  are leaf-additive, but ``cpa_ref``/``cogs_ref``/``ltv_ref``/``margin_ref`` are *unit*
  values repeated on each leaf. Summing the latter across a unit's leaves double-counts.

Every join logs matched/unmatched counts; an unexpected miss (a GL line that doesn't
resolve — which the validator should already have caught) logs at WARNING.
"""

from __future__ import annotations

import logging

import pandas as pd

# The 8-field dimension hierarchy is the leaf join identity — reuse the validator's
# canonical list so the two modules can never drift on column set or order.
from app.validation.ingestion_validator import DIMENSION_COLUMNS

logger = logging.getLogger(__name__)

# ===========================================================================
# 1. Join keys and reference column groups
# ===========================================================================
DIMS = list(DIMENSION_COLUMNS)                       # leaf grain
GL_KEY = ["cost_center", "gl_account", "vendor"]     # the dimension-free ledger's key
GL_MAP_ATTACHED = ["segment", "entity", "region", "spend_category"]  # added by the join

# Reference (plan/forecast) columns attached to each actuals frame, by feed.
SALES_REF_COLS = ["volume_in_ref"]
CONV_REF_COLS = ["volume_converted_ref", "cost_ref", "cpa_ref",
                 "cogs_ref", "ltv_ref", "margin_ref"]

ACQUISITION = "acquisition_marketing"


# ===========================================================================
# 2. Small helpers — period derivation, logged joins, reference slices
# ===========================================================================
def _period(s: pd.Series) -> pd.Series:
    """Calendar-month key ``YYYY-MM`` from a datetime column."""
    return s.dt.to_period("M").astype(str)


def _reference_at_period(reference: pd.DataFrame, rtype: str, cols: list[str],
                         suffix: str = "") -> pd.DataFrame:
    """One reference type (plan/forecast) as a leaf×period frame with ``cols`` selected.

    Guards 1:1-ness: if a (leaf, period) somehow carries more than one row of this type
    the join would multiply, so duplicates are dropped (keeping the first) with a warning.
    """
    r = reference[reference["reference_type"] == rtype].copy()
    r["period"] = _period(r["date"])
    keys = DIMS + ["period"]
    dups = int(r.duplicated(keys).sum())
    if dups:
        logger.warning("reference_data: %d duplicate %s row(s) at (leaf, period) — "
                       "keeping first", dups, rtype)
        r = r.drop_duplicates(keys)
    out = r[keys + cols]
    if suffix:
        out = out.rename(columns={c: c + suffix for c in cols})
    return out


def _join_reference(actuals: pd.DataFrame, reference: pd.DataFrame, ref_cols: list[str],
                    label: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """LEFT JOIN an aggregated actuals frame (leaf×period) to plan, then attach forecast.

    Returns ``(joined, unmatched)`` where ``joined`` gains a boolean ``has_plan`` flag and
    the forecast columns (``*_fc``), and ``unmatched`` is the distinct (leaf×period) rows
    with no plan row — the new-leaf / first-run signal (§9). Logs match counts.
    """
    keys = DIMS + ["period"]
    plan = _reference_at_period(reference, "plan", ref_cols)
    joined = actuals.merge(plan, on=keys, how="left", indicator=True)
    joined["has_plan"] = joined["_merge"] == "both"
    n_matched = int(joined["has_plan"].sum())
    n_missing = int((~joined["has_plan"]).sum())
    logger.info("%s → plan: %d matched, %d with no plan row (leaf×period)",
                label, n_matched, n_missing)

    unmatched = joined.loc[~joined["has_plan"], keys].reset_index(drop=True)
    joined = joined.drop(columns="_merge")

    forecast = _reference_at_period(reference, "forecast", ref_cols, suffix="_fc")
    joined = joined.merge(forecast, on=keys, how="left")
    return joined, unmatched


# ===========================================================================
# 3. The four build steps
# ===========================================================================
def _sales_with_ref(frames: dict, reference: pd.DataFrame):
    """Submissions aggregated to leaf×period (``volume_in``), joined to plan/forecast."""
    sales = frames["sales"].copy()
    sales["period"] = _period(sales["sale_date"])
    agg = (sales.groupby(DIMS + ["period"], observed=True)
                .size().reset_index(name="volume_in"))
    return _join_reference(agg, reference, SALES_REF_COLS, "sales")


def _conversions_with_ref(frames: dict, reference: pd.DataFrame) -> pd.DataFrame:
    """Gains aggregated to leaf×period on **both** date axes, plus price aggregates,
    joined to plan/forecast. ``volume_converted_landed`` counts by ``conversion_date``
    (gains in period); ``volume_converted_cohort`` by ``sale_date`` (submission cohort)."""
    conv = frames["conversions"].copy()

    # Landed axis (conversion month) — also where price is summarized.
    conv["period"] = _period(conv["conversion_date"])
    landed = (conv.groupby(DIMS + ["period"], observed=True)
                  .agg(volume_converted_landed=("customer_key", "size"),
                       price_sum=("price_per_unit", "sum"),
                       price_mean=("price_per_unit", "mean"),
                       priced_gains=("price_per_unit", "count"))
                  .reset_index())

    # Cohort axis (sale month) — for fallout-consistent conversion counts.
    conv["period"] = _period(conv["sale_date"])
    cohort = (conv.groupby(DIMS + ["period"], observed=True)
                  .size().reset_index(name="volume_converted_cohort"))

    agg = landed.merge(cohort, on=DIMS + ["period"], how="outer")
    for col in ("volume_converted_landed", "volume_converted_cohort", "priced_gains"):
        agg[col] = agg[col].fillna(0).astype("int64")
    agg["unpriced_gains"] = agg["volume_converted_landed"] - agg["priced_gains"]
    agg["price_sum"] = agg["price_sum"].fillna(0.0)  # price_mean stays NaN when unpriced

    joined, _ = _join_reference(agg, reference, CONV_REF_COLS, "conversions")
    return joined


def _fallout(frames: dict) -> pd.DataFrame:
    """Reconcile sales↔conversions on ``customer_key`` at leaf × cohort-period.

    ``unmatched`` = submissions with no gain *in this snapshot*. For recent cohorts that
    includes gains still within the conversion lag (not yet landed) — fallout only fully
    resolves post-close (data dictionary). The merger reports the reconciliation; the
    lag-aware fallout *rate* is computed downstream."""
    sales = frames["sales"].copy()
    converted_keys = frames["conversions"]["customer_key"]
    sales["period"] = _period(sales["sale_date"])
    sales["converted"] = sales["customer_key"].isin(converted_keys)

    out = (sales.groupby(DIMS + ["period"], observed=True)
                .agg(submissions=("customer_key", "size"),
                     matched=("converted", "sum"))
                .reset_index())
    out["matched"] = out["matched"].astype("int64")
    out["unmatched"] = out["submissions"] - out["matched"]
    logger.info("fallout: %d submissions, %d matched, %d unmatched (this snapshot)",
                int(out["submissions"].sum()), int(out["matched"].sum()),
                int(out["unmatched"].sum()))
    return out


def _gl_resolved(frames: dict):
    """Resolve the dimension-free ledger to geography via ``gl_mapping`` (1:1 on the GL
    key), returning ``(gl_full, gl_acquisition, unmatched_gl)``."""
    gl = frames["gl_actuals"]
    gl_map = frames["gl_mapping"][GL_KEY + GL_MAP_ATTACHED]
    gl_full = gl.merge(gl_map, on=GL_KEY, how="left", indicator=True)

    resolved = gl_full["_merge"] == "both"
    unmatched_gl = gl_full.loc[~resolved].drop(columns="_merge").reset_index(drop=True)
    if len(unmatched_gl):
        # The validator should already have halted on this; defensive guard.
        logger.warning("gl_actuals: %d line(s) did not resolve via gl_mapping — "
                       "should have been caught at ingestion", len(unmatched_gl))
    gl_full = gl_full.drop(columns="_merge")
    gl_acquisition = gl_full[gl_full["spend_category"] == ACQUISITION].reset_index(drop=True)
    logger.info("gl_actuals → gl_mapping: %d resolved, %d unresolved; %d acquisition line(s)",
                int(resolved.sum()), len(unmatched_gl), len(gl_acquisition))
    return gl_full, gl_acquisition, unmatched_gl


# ===========================================================================
# 4. Public entry point
# ===========================================================================
def merge_frames(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Join the cleaned feeds to their reference dimensions and GL mapping.

    Expects the dict from ``data_loader.load_data`` (cleaned, normalized string keys).
    Returns seven frames::

        sales_with_ref        leaf×period: volume_in vs plan/forecast (+ has_plan)
        conversions_with_ref  leaf×period: gains (landed & cohort) + price vs plan/forecast
        fallout               leaf×cohort-period: submissions / matched / unmatched
        gl_full               GL lines with geography + spend_category attached
        gl_acquisition        gl_full filtered to acquisition_marketing (overhead excluded)
        unmatched_sales       distinct leaf×period in sales with no plan row (first-run)
        unmatched_gl          GL lines that didn't resolve (defensive; expected empty)
    """
    reference = frames["reference_data"]
    sales_with_ref, unmatched_sales = _sales_with_ref(frames, reference)
    conversions_with_ref = _conversions_with_ref(frames, reference)
    fallout = _fallout(frames)
    gl_full, gl_acquisition, unmatched_gl = _gl_resolved(frames)

    return {
        "sales_with_ref": sales_with_ref,
        "conversions_with_ref": conversions_with_ref,
        "fallout": fallout,
        "gl_full": gl_full,
        "gl_acquisition": gl_acquisition,
        "unmatched_sales": unmatched_sales,
        "unmatched_gl": unmatched_gl,
    }


# ===========================================================================
# 5. CLI — manual verification aid (load → merge → print shapes)
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from app.analytics.data_loader import load_data

    merged = merge_frames(load_data())
    print("Merged frames (key: rows × cols):")
    for key, df in merged.items():
        print(f"  {key:22s} {df.shape[0]:>7,} × {df.shape[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
