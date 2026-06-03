"""gen_conversions.py — produces conversions.csv (record-level gains).

The gains feed: one row per submission that converted. This module owns the
*gain decision* — for each submission in the sales feed it draws (from the
series' per-day conversion rate) whether that sale becomes a gain. The gains it
emits carry the submission's `customer_key` and `sale_date` plus a
`conversion_date` (= sale_date + a drawn lag) and `price_per_unit`.

Fallout is NOT stored anywhere — it is the complement: any submission in
sales.csv without a matching conversion (by `customer_key`) fell out. Only the
fallout series degrades its conversion rate across the active period, so its
share of unmatched submissions climbs.

Dimensions are denormalized onto the gain (matching the submission) so the file
is self-describing; `customer_key` remains the authoritative link back to sales.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import shared

# Output column order (dims slot in via Series.dims()).
_LEAD = ["customer_key", "sale_date", "conversion_date"]


def _conv_rate_for(series, d: dt.date, config) -> float:
    """Per-day probability a submission converts. Only the fallout series
    degrades, and only inside the active period after the configured start day —
    ramping the fallout rate (1 - conv_rate) up toward the target by the final
    (confirmed) snapshot day, then holding."""
    base = series.base_conv_rate
    if series.role != "fallout" or d < shared.ACTIVE_PERIOD_START:
        return base
    if d.day < config.fallout_start_day:
        return base
    full_day = shared.LAST_INMONTH_SNAPSHOT.day
    span = max(1, full_day - config.fallout_start_day)
    frac = min(1.0, (d.day - config.fallout_start_day) / span)
    target_conv = 1.0 - config.fallout_target_rate
    return base - frac * (base - target_conv)


def generate(config, sales_df: pd.DataFrame) -> pd.DataFrame:
    """Return the conversions dataframe: the subset of submissions that convert,
    each with a conversion_date and price_per_unit."""
    # Tag each submission with its series key so we can process per series (and
    # so None/NaN dimension cells compare correctly via the shared normalizer).
    keys = [shared.normalize_dim_tuple(t)
            for t in sales_df[shared.DIMENSION_COLUMNS].itertuples(index=False, name=None)]
    sales = sales_df.assign(_key=keys)

    frames = []
    for s in shared.SERIES:
        sub = sales[sales["_key"].map(lambda k, key=s.key: k == key)].sort_values("customer_key")
        n = len(sub)
        if n == 0:
            continue

        # One RNG stream per series; fixed draw order: gain decisions, then lag,
        # then price — so the output is byte-reproducible.
        rng = shared.make_rng(f"conversions:{s.cost_center}")

        # Gain decision: per submission, by its sale_date's conversion rate.
        sale_dates = [dt.date.fromisoformat(x) for x in sub["sale_date"].tolist()]
        p_gain = np.array([_conv_rate_for(s, d, config) for d in sale_dates])
        gained = rng.random(n) < p_gain
        ng = int(gained.sum())
        if ng == 0:
            continue
        g = sub[gained]
        g_sale_dates = [d for d, keep in zip(sale_dates, gained) if keep]

        # conversion_date = sale_date + a drawn lag (clamped).
        lag = np.clip(
            rng.poisson(config.conv_lag_mean_days, ng),
            config.conv_lag_min_days, config.conv_lag_max_days,
        )
        conversion_date = [
            (d + dt.timedelta(days=int(l))).isoformat() for d, l in zip(g_sale_dates, lag)
        ]

        # Price per unit — null for the missing-price series (margin fallback).
        if s.base_price_per_unit is None:
            price = [None] * ng
        else:
            price = np.round(
                shared.apply_noise(np.full(ng, s.base_price_per_unit), rng, config.noise_sd * 0.5),
                2,
            ).tolist()

        frames.append(pd.DataFrame({
            "customer_key": g["customer_key"].values,
            "sale_date": g["sale_date"].values,
            "conversion_date": conversion_date,
            **{col: val for col, val in s.dims().items()},
            "price_per_unit": price,
        }))

    cols = _LEAD + shared.DIMENSION_COLUMNS + ["price_per_unit"]
    if not frames:
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)[cols].sort_values("customer_key").reset_index(drop=True)
