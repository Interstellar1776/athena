"""gen_reference.py — produces reference_data.csv (plan + forecast targets).

Plan and forecast share one schema, distinguished by reference_type. Plan rows
are emitted at monthly grain (first of month) for EVERY series across its
history — including the brand-new ERCOT West series, so first-run fallback
always has a plan to fall back to. Forecast rows are emitted only for series
flagged has_forecast, dated at their mid-period issue date so they appear from
the May-15 snapshot onward (simulating a rolling forecast arriving mid-period)
and diverge from plan to exercise the plan-vs-forecast-gap alert.

Plan volumes are the noise-free baseline summed over the month, so plan ~= the
unperturbed actual baseline and variance comes from the engineered spike/fallout
plus noise — not from an arbitrary plan offset.
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
        # Plan rows for every month the series exists.
        for m in shared.month_starts(s.effective_history_start, shared.ACTIVE_PERIOD_END):
            vin, vconv = _baseline_monthly_volumes(s, m)
            rows.append({
                "date": m.isoformat(),
                **s.dims(),
                "reference_type": "plan",
                "volume_in_ref": int(round(vin)),
                "volume_converted_ref": int(round(vconv)),
                "cost_ref": round(s.base_cpa * vconv, 2),
                "cpa_ref": round(s.base_cpa, 2),
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
