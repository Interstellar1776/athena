"""gen_sales.py — produces sales.csv (record-level submissions / enrollments).

The submissions feed: one row per sale (enrollment), the day it was sold. It is
deliberately "dumb" — a submission does NOT know its own fate. Whether a sale
becomes a gain is decided downstream by gen_conversions; fallout is then derived
by anti-join (a sale with no matching conversion on `customer_key` fell out).
Keeping outcome off this feed is what prevents a snapshot from revealing the
result of an enrollment that, at that point in time, is still pending.

It iterates shared.SERIES only, so every dimension tuple it emits is a valid
join key. `customer_key` is the surrogate key that links a submission to its
conversion; each series numbers within its own block (see shared) so keys stay
plain integers yet stable when one series' volume is retuned.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import shared

_LEAD = ["customer_key", "sale_date"]


def generate(config) -> pd.DataFrame:
    """Return the full-timeline submissions dataframe for every series.

    One row per submission: customer_key, sale_date, the full dimension tuple."""
    frames = []
    for idx, s in enumerate(shared.SERIES):
        # One RNG stream per series keeps draws isolated and reproducible.
        rng = shared.make_rng(f"sales:{s.cost_center}")
        dates = list(shared.daterange(s.effective_history_start, shared.ACTIVE_PERIOD_END))

        # Daily submission count = baseline volume shaped by seasonality +
        # day-of-week, perturbed by small multiplicative noise.
        base_volume = np.array([
            s.base_volume_in * shared.seasonal_factor(d) * shared.weekday_factor(d, s.segment)
            for d in dates
        ])
        daily_count = shared.clamp_nonneg(
            np.round(shared.apply_noise(base_volume, rng, config.noise_sd))
        ).astype(int)

        n = int(daily_count.sum())
        if n == 0:
            continue

        # customer_key: this series' block base + a 1-based local index.
        base_key = (idx + 1) * shared.CUSTOMER_KEY_BLOCK_SIZE
        customer_key = base_key + np.arange(1, n + 1)
        # Expand each date by its submission count (ascending sale_date order).
        sale_date = np.repeat([d.isoformat() for d in dates], daily_count)

        frames.append(pd.DataFrame({
            "customer_key": customer_key,
            "sale_date": sale_date,
            **{col: val for col, val in s.dims().items()},
        }))

    cols = _LEAD + shared.DIMENSION_COLUMNS
    if not frames:
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)[cols]
