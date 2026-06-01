"""gen_sales.py — produces actuals.csv (operational volume & revenue).

The spine of the system: daily units in / converted / lost, plus revenue per
unit. Iterates shared.SERIES only, so every (entity, segment, product_type) it
emits is a valid join key. The CPA spike is NOT created here — it lives in GL
spend (gen_gl). Here the hero's conversions stay on their baseline; the fallout
series is the only one whose conversion rate is deliberately degraded.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import shared


def _conv_rate_for(series, d, config) -> float:
    """Per-day conversion rate. Only the fallout series degrades, and only
    inside the active period after the configured start day — ramping the
    fallout rate (1 - conv_rate) up toward the target."""
    base = series.base_conv_rate
    if series.role != "fallout" or d < shared.ACTIVE_PERIOD_START:
        return base
    if d.day < config.fallout_start_day:
        return base
    # Ramp the daily conversion rate down so the fallout rate reaches its target
    # by the final (confirmed) snapshot day, then holds — so the month-to-date
    # fallout rate is fully developed at the snapshot that matters.
    full_day = shared.SNAPSHOT_DATES[-1].day
    span = max(1, full_day - config.fallout_start_day)
    frac = min(1.0, (d.day - config.fallout_start_day) / span)
    target_conv = 1.0 - config.fallout_target_rate
    return base - frac * (base - target_conv)


def generate(config) -> pd.DataFrame:
    """Return the full-timeline actuals dataframe for every series."""
    rows = []
    for s in shared.SERIES:
        # One RNG stream per series keeps draws isolated and reproducible.
        rng = shared.make_rng(f"sales:{s.cost_center}")
        dates = list(shared.daterange(s.effective_history_start, shared.ACTIVE_PERIOD_END))
        n = len(dates)

        # Baseline daily inbound volume, shaped by seasonality + day-of-week,
        # then perturbed by small multiplicative noise.
        base_volume = np.array([
            s.base_volume_in * shared.seasonal_factor(d) * shared.weekday_factor(d, s.segment)
            for d in dates
        ])
        volume_in = shared.clamp_nonneg(np.round(shared.apply_noise(base_volume, rng, config.noise_sd)))

        # Conversions = volume_in x conversion-rate (gently noised), clamped so
        # the data-dictionary invariant volume_converted <= volume_in always holds.
        conv_rate = np.array([_conv_rate_for(s, d, config) for d in dates])
        conv_noise = 1.0 + rng.normal(0.0, config.noise_sd * 0.5, size=n)
        volume_converted = shared.clamp_nonneg(np.minimum(np.round(volume_in * conv_rate * conv_noise), volume_in))
        volume_lost = volume_in - volume_converted

        # Revenue per unit — null for the missing-revenue series (margin fallback).
        if s.base_revenue_per_unit is None:
            revenue = [None] * n
        else:
            rev_arr = shared.apply_noise(np.full(n, s.base_revenue_per_unit), rng, config.noise_sd * 0.5)
            revenue = np.round(rev_arr, 2)

        for i, d in enumerate(dates):
            rows.append({
                "date": d.isoformat(),
                "entity": s.entity,
                "segment": s.segment,
                "product_type": s.product_type,
                "volume_in": int(volume_in[i]),
                "volume_converted": int(volume_converted[i]),
                "volume_lost": int(volume_lost[i]),
                "revenue_per_unit": (None if s.base_revenue_per_unit is None else float(revenue[i])),
            })

    return pd.DataFrame(rows)
