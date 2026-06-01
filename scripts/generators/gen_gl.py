"""gen_gl.py — produces gl_actuals.csv (raw general-ledger spend).

Spend entries are the source of CPA actuals and of late-invoice detection.
Acquisition spend is calibrated so baseline (cumulative spend / cumulative
conversions) ~= the series' plan CPA. The hero's CPA spike is engineered HERE,
by ramping spend across the active period while gen_sales holds conversions flat.

Two spend-shaping schemes:
  • history (all series) + active period (non-hero): a per-day multiplier that
    rises gently across history (slow unit-economics drift), flat in the active
    period — so non-hero CPA tracks plan.
  • hero, active period: a CUMULATIVE-TARGET scheme. Daily spend is back-solved
    so the cumulative-to-date CPA hits the arc anchors exactly (before noise):
    ~+1% at day 1, +8% at day 8, +15% at day 15, +22% at day 22. That is what
    makes each cumulative snapshot land on its intended point in the §8 arc.

One engineered late/accrued entry posts in May against an April document_date,
so it appears only in the May-22 snapshot (cumulative cut by posting_date) and
exercises the accrued/restated GL completeness states.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import shared

# Shape of the hero's cumulative CPA-vs-plan spike across the active period,
# expressed as a FRACTION of the full spike magnitude (cpa_spike_magnitude) at
# each day. The day-22 anchor is 1.0 (full magnitude at the confirmed snapshot);
# it overshoots slightly afterward. Linear-interpolated between anchors.
_HERO_ARC_DAYS = np.array([1, 8, 15, 22, 31])
_HERO_ARC_FRAC = np.array([0.05, 0.36, 0.68, 1.00, 1.22])

# Vendor per channel (free text; descriptions feed the retrieval layer later).
_VENDORS = {
    "Paid Search": "SearchAds Media",
    "Door_to_Door": "FieldForce Sales LLC",
    "Broker": "ChannelPartners Brokerage",
    "Affiliate": "AffiliateHub Network",
}
_OVERHEAD_VENDOR = "Corporate Allocations"


def _history_multiplier(series, d) -> float:
    """Spend-per-conversion multiplier for history dates: rises linearly from
    (1 - drift) at the series' history start to 1.0 at the active period start
    (slow unit-economics compression). 1.0 once inside the active period."""
    hs = series.effective_history_start
    total = (shared.ACTIVE_PERIOD_START - hs).days
    if total <= 0 or d >= shared.ACTIVE_PERIOD_START:
        return 1.0
    frac = (d - hs).days / total
    return (1.0 - series.cpa_history_drift) + series.cpa_history_drift * frac


def _expected_conv(series, d) -> float:
    """Deterministic (noise-free) expected conversions for a day — the basis for
    pricing spend so CPA averages to plan."""
    return (series.base_volume_in * series.base_conv_rate
            * shared.seasonal_factor(d) * shared.weekday_factor(d, series.segment))


def generate(config) -> pd.DataFrame:
    """Return the full-timeline GL dataframe for every series, plus the late entry."""
    rows = []
    for s in shared.SERIES:
        rng = shared.make_rng(f"gl:{s.cost_center}")
        dates = list(shared.daterange(s.effective_history_start, shared.ACTIVE_PERIOD_END))
        active_dates = [d for d in dates if d >= shared.ACTIVE_PERIOD_START]

        # --- daily acquisition spend ---
        spend = {}
        # history (all) + active period (non-hero): per-day multiplier
        for d in dates:
            if s.role == "hero" and d >= shared.ACTIVE_PERIOD_START:
                continue  # hero active period handled by cumulative-target below
            spend[d] = s.base_cpa * _expected_conv(s, d) * _history_multiplier(s, d)

        # hero active period: back-solve daily spend from the cumulative CPA arc
        if s.role == "hero" and active_dates:
            exp_conv = np.array([_expected_conv(s, d) for d in active_dates])
            day_of_month = np.array([d.day for d in active_dates])
            # Cumulative uplift = arc shape (fraction) x configured spike magnitude.
            cum_uplift = np.interp(day_of_month, _HERO_ARC_DAYS, _HERO_ARC_FRAC) * config.cpa_spike_magnitude
            cum_conv = np.cumsum(exp_conv)
            cum_spend = s.base_cpa * (1.0 + cum_uplift) * cum_conv
            daily = np.diff(cum_spend, prepend=0.0)
            for d, val in zip(active_dates, daily):
                spend[d] = val

        # Emit acquisition rows (noise applied to daily spend; cumulative CPA is
        # preserved because zero-mean noise averages out across the period).
        ordered = sorted(spend.keys())
        amounts = shared.clamp_nonneg(shared.apply_noise([spend[d] for d in ordered], rng, config.noise_sd))
        for d, amt in zip(ordered, amounts):
            rows.append({
                "posting_date": d.isoformat(),
                "document_date": d.isoformat(),
                "cost_center": s.cost_center,
                "gl_account": shared.GL_ACCOUNT_ACQUISITION,
                "amount": round(float(amt), 2),
                "vendor": _VENDORS[s.segment],
                "description": f"{s.segment} acquisition spend — {shared.month_str(d)}",
            })

        # Monthly overhead entries — present so gl_mapping's spend_category split
        # is exercised; mapped OUT of CPA downstream.
        for m in shared.month_starts(s.effective_history_start, shared.ACTIVE_PERIOD_END):
            overhead = s.base_cpa * s.base_volume_in * s.base_conv_rate * 2.0  # ~2 days' acq as monthly overhead
            overhead = float(shared.clamp_nonneg(shared.apply_noise(overhead, rng, config.noise_sd)))
            rows.append({
                "posting_date": m.isoformat(),
                "document_date": m.isoformat(),
                "cost_center": s.cost_center,
                "gl_account": shared.GL_ACCOUNT_OVERHEAD,
                "amount": round(overhead, 2),
                "vendor": _OVERHEAD_VENDOR,
                "description": f"Monthly overhead allocation — {shared.month_str(m)}",
            })

    # --- late / accrued invoice (single engineered entry) ---
    # April document_date, May posting_date -> appears only in the May-22 snapshot
    # and adds to the confirmed hero spike while exercising the accrued state.
    if config.late_invoice_enabled:
        rows.append({
            "posting_date": config.late_invoice_posting_date.isoformat(),
            "document_date": config.late_invoice_doc_date.isoformat(),
            "cost_center": config.late_invoice_cost_center,
            "gl_account": shared.GL_ACCOUNT_ACQUISITION,
            "amount": round(float(config.late_invoice_amount), 2),
            "vendor": _VENDORS["Paid Search"],
            "description": "Late vendor invoice for April paid-search overage — posted in May (prior-period accrual)",
        })

    return pd.DataFrame(rows)
