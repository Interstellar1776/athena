"""gen_reference.py — produces reference_data.csv (plan + forecast targets).

Plan and forecast share one schema, distinguished by reference_type, at monthly
grain (first of month / mid-period issue date). Rows are emitted per LEAF series
across each series' history — so the table is the baseline the variance engine
compares actuals against, and the bottom of every fallback chain (§9-10).

GRAIN CONTRACT (so plan ties back to the actuals AND the GL):
  • Volume targets (volume_in_ref, volume_converted_ref) are at SUB-SEGMENT (leaf)
    grain — directly comparable to the record-level sales/conversions aggregated
    per sub-segment + month.
  • Cost/CPA targets are a UNIT (channel×geography) plan: cpa_ref is the unit's
    plan CPA, and cost_ref = cpa_ref × planned conversions is the unit's plan
    cost ALLOCATED across its leaves by planned conversions. So
    Σ leaf cost_ref over (entity, region, segment) = the unit's plan cost, which
    reconciles to actual GL spend — gl_mapping resolves GL acquisition spend to
    the same (entity, region, segment). cpa_ref is the plan CPA to compare
    against actual (GL spend ÷ conversions) at that unit grain.

Plan volumes/CPA default to the noise-free baseline (plan == unperturbed actual
baseline), so the engineered spike/fallout is the only signal. A per-unit
`plan_bias` ({"volume":…, "cpa":…}) can make the plan deviate independently
(a real plan misses); empty by default.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import shared

# Forecast assumes slightly fewer conversions than plan (a realistic haircut).
_FORECAST_VOLUME_HAIRCUT = 0.03


def _month_end(month_start: dt.date) -> dt.date:
    if month_start.month == 12:
        nxt = month_start.replace(year=month_start.year + 1, month=1)
    else:
        nxt = month_start.replace(month=month_start.month + 1)
    return nxt - dt.timedelta(days=1)


def _baseline_monthly_volumes(series, month_start: dt.date):
    """Noise-free monthly (volume_in, volume_converted) for a series — plan basis."""
    days = list(shared.daterange(month_start, _month_end(month_start)))
    vin = sum(series.base_volume_in * shared.seasonal_factor(d) * shared.weekday_factor(d, series.segment)
              for d in days)
    return vin, vin * series.base_conv_rate


def generate(config) -> pd.DataFrame:
    """Return the full-timeline reference dataframe (plan rows + forecast rows)."""
    rows = []
    for s in shared.SERIES:
        # Per-unit plan error off the baseline (default 0 -> plan == baseline).
        vol_bias = s.plan_bias.get("volume", 0.0)
        cpa_bias = s.plan_bias.get("cpa", 0.0)
        plan_cpa = s.base_cpa * (1.0 + cpa_bias)

        # Plan rows for every month the series exists.
        for m in shared.month_starts(s.effective_history_start, shared.ACTIVE_PERIOD_END):
            vin, vconv = _baseline_monthly_volumes(s, m)
            vin *= (1.0 + vol_bias)
            vconv *= (1.0 + vol_bias)
            rows.append({
                "date": m.isoformat(),
                **s.dims(),
                "reference_type": "plan",
                "volume_in_ref": int(round(vin)),
                "volume_converted_ref": int(round(vconv)),
                "cost_ref": round(plan_cpa * vconv, 2),
                "cpa_ref": round(plan_cpa, 2),
                "cogs_ref": round(s.base_cogs_per_unit, 2),
                "ltv_ref": round(s.plan_ltv, 2),
                "margin_ref": round(s.base_margin_per_unit, 2),
            })

        # Forecast row for the active month — issued mid-period, diverging on CPA.
        if s.has_forecast:
            m = shared.ACTIVE_PERIOD_START
            vin, vconv = _baseline_monthly_volumes(s, m)
            vconv *= (1.0 - _FORECAST_VOLUME_HAIRCUT)
            f_cpa = s.base_cpa * (1.0 + config.forecast_divergence)
            rows.append({
                "date": config.forecast_issue_date.isoformat(),
                **s.dims(),
                "reference_type": "forecast",
                "volume_in_ref": int(round(vin)),
                "volume_converted_ref": int(round(vconv)),
                "cost_ref": round(f_cpa * vconv, 2),
                "cpa_ref": round(f_cpa, 2),
                "cogs_ref": round(s.base_cogs_per_unit, 2),
                "ltv_ref": round(s.plan_ltv, 2),
                "margin_ref": round(s.base_margin_per_unit, 2),
            })

    return pd.DataFrame(rows)
