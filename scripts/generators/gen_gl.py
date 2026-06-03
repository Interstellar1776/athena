"""gen_gl.py — produces gl_actuals.csv (raw general-ledger spend).

A dimension-free ledger: each row is cost_center (WHO spent — the channel),
gl_account (WHAT — media / hours / commissions / bonuses / overhead), amount,
vendor, and descriptions. No entity/region/segment columns — those are
reconstructed downstream via config/gl_mapping.csv keyed on
(cost_center, gl_account, vendor) (see shared.canonical_gl_mapping_rows).

Spend is emitted as **periodic invoices** (per-channel cadence), billed in
arrears (posting on the last day of the covered span). The pipeline:
  1. Compute a noise-free DAILY spend target per channel×geography unit
     (cumulative spend / expected conversions ~= plan CPA; the hero's active
     period is back-solved to the cumulative-CPA arc so each snapshot lands on
     its intended point).
  2. Aggregate those daily targets into invoices by cadence (weekly / monthly),
     carve out a bonus slice where the channel uses a bonus account, and split
     each invoice across the unit's vendor(s).
  3. Add a token monthly overhead stream, the engineered late April invoice, and
     the post-close May restatement (both against the hero channel).

Cadence drives the demo's GL-completeness states: weekly channels show spend
progress across snapshots (hero spike visible); monthly channels have no
in-month spend until month-end, so their CPA falls back to an estimate early and
only settles in the post-close snapshot.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import shared

# Shape of the hero's cumulative CPA-vs-plan spike across the active period, as a
# FRACTION of the full spike magnitude at each day (day-22 = 1.0; overshoots
# slightly after). Linear-interpolated between anchors.
_HERO_ARC_DAYS = np.array([1, 8, 15, 22, 31])
_HERO_ARC_FRAC = np.array([0.05, 0.36, 0.68, 1.00, 1.22])

# Weekly invoice spans within a month: (lo_day, hi_day) inclusive. The invoice
# posts on the last present day of its span (arrears — never before the spend).
_WEEKLY_BOUNDS = [(1, 8), (9, 15), (16, 22), (23, 31)]

_OUT_COLS = [
    "posting_date", "document_date", "cost_center", "cost_center_description",
    "gl_account", "gl_account_description", "amount", "vendor", "description",
]


def _expected_conv(series, d) -> float:
    """Deterministic (noise-free) expected conversions for a day — the basis for
    pricing spend so CPA averages to plan."""
    return (series.base_volume_in * series.base_conv_rate
            * shared.seasonal_factor(d) * shared.weekday_factor(d, series.segment))


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


def _daily_spend(series, config) -> dict:
    """Noise-free daily acquisition spend (total across accounts) for a unit.
    History + non-hero active: base_cpa x expected_conv x history drift. Hero
    active period: back-solved from the cumulative-CPA arc."""
    dates = list(shared.daterange(series.effective_history_start, shared.ACTIVE_PERIOD_END))
    active = [d for d in dates if d >= shared.ACTIVE_PERIOD_START]
    spend = {}
    for d in dates:
        if series.role == "hero" and d >= shared.ACTIVE_PERIOD_START:
            continue  # hero active period handled by the arc below
        spend[d] = series.base_cpa * _expected_conv(series, d) * _history_multiplier(series, d)

    if series.role == "hero" and active:
        exp_conv = np.array([_expected_conv(series, d) for d in active])
        day_of_month = np.array([d.day for d in active])
        cum_uplift = np.interp(day_of_month, _HERO_ARC_DAYS, _HERO_ARC_FRAC) * config.cpa_spike_magnitude
        cum_spend = series.base_cpa * (1.0 + cum_uplift) * np.cumsum(exp_conv)
        for d, val in zip(active, np.diff(cum_spend, prepend=0.0)):
            spend[d] = val
    return spend


def _invoice_spans(present_days: list, cadence: str):
    """Yield (posting_date, [covered days]) for one month's present days, billed
    in arrears (posting = last covered day)."""
    if not present_days:
        return
    if cadence == "weekly":
        for lo, hi in _WEEKLY_BOUNDS:
            days = [d for d in present_days if lo <= d.day <= hi]
            if days:
                yield max(days), days
    else:  # monthly — one invoice at month end
        yield max(present_days), present_days


def _emit(rows, posting, document, cost_center, cc_desc, account, amount, vendor):
    """Append one ledger row (amount already noised/clamped)."""
    rows.append({
        "posting_date": posting.isoformat(),
        "document_date": document.isoformat(),
        "cost_center": cost_center,
        "cost_center_description": cc_desc,
        "gl_account": account,
        "gl_account_description": shared.GL_ACCOUNTS[account][0],
        "amount": round(float(amount), 2),
        "vendor": vendor,
        "description": f"{cc_desc} — {shared.GL_ACCOUNTS[account][0]} — {shared.month_str(posting)}",
    })


def generate(config) -> pd.DataFrame:
    """Return the full-timeline raw GL ledger for every channel×geography unit,
    plus overhead, the late invoice and the post-close restatement."""
    rows = []

    # Iterate UNITS, not leaf SERIES — spend is at the channel×geography grain
    # (the mapping resolves there); the leaf product/customer mix only affects the
    # record-level sales/conversions feeds.
    for s in shared.UNITS:
        rng = shared.make_rng(f"gl:{s.segment}:{s.entity}:{s.region}")
        daily = _daily_spend(s, config)
        n_vendors = len(s.vendors)
        is_hero = s.role == "hero"
        # Non-hero bonus is CARVED from acquisition spend (total stays plan-level).
        # The hero's bonus is instead an ADDITIVE active-period incentive surge
        # (below) — carving it would flatten the visible pre-close commission spike.
        carve_frac = shared.BONUS_FRACTION if (s.bonus_account and not is_hero) else 0.0

        # Group present days by calendar month, emit invoices per cadence.
        months = sorted({dt.date(d.year, d.month, 1) for d in daily})
        for m in months:
            month_days = sorted(d for d in daily if (d.year, d.month) == (m.year, m.month))

            # Primary-account invoices (cadence-paced), split across vendors.
            for posting, covered in _invoice_spans(month_days, s.cadence):
                base = sum(daily[d] for d in covered) * (1.0 - carve_frac)
                for vendor in s.vendors:
                    amt = shared.clamp_nonneg(
                        shared.apply_noise(base / n_vendors, rng, config.noise_sd))
                    if amt > 0:
                        _emit(rows, posting, posting, s.cost_center,
                              s.cost_center_description, s.gl_account, amt, vendor)

            # Bonus-account invoice (one per month, month-end), split across vendors.
            if s.bonus_account:
                month_total = sum(daily[d] for d in month_days)
                if is_hero:
                    # Additive Q2 incentive surge — active-period month(s) only.
                    active = (m.year, m.month) == (shared.ACTIVE_PERIOD_START.year,
                                                   shared.ACTIVE_PERIOD_START.month)
                    bonus_total = month_total * config.hero_incentive_surge_frac if active else 0.0
                else:
                    bonus_total = month_total * carve_frac
                if bonus_total > 0:
                    posting = max(month_days)
                    for vendor in s.vendors:
                        amt = shared.clamp_nonneg(
                            shared.apply_noise(bonus_total / n_vendors, rng, config.noise_sd))
                        if amt > 0:
                            _emit(rows, posting, posting, s.cost_center,
                                  s.cost_center_description, s.bonus_account, amt, vendor)

    # --- token monthly overhead (mapped out of CPA) ---
    cc, cc_desc = shared.OVERHEAD_COST_CENTER
    o_rng = shared.make_rng("gl:overhead")
    for m in shared.month_starts(shared.HISTORY_START, shared.ACTIVE_PERIOD_END):
        posting = _month_end(m)
        amt = float(shared.clamp_nonneg(shared.apply_noise(6000.0, o_rng, config.noise_sd)))
        _emit(rows, posting, posting, cc, cc_desc, shared.OVERHEAD_ACCOUNT, amt, shared.OVERHEAD_VENDOR)

    # --- engineered hero entries: late April invoice + post-close May restatement ---
    hero = next(s for s in shared.SERIES if s.role == "hero")
    hv = hero.vendors[0]
    if config.late_invoice_enabled:
        _emit(rows, config.late_invoice_posting_date, config.late_invoice_doc_date,
              hero.cost_center, hero.cost_center_description, hero.gl_account,
              config.late_invoice_amount, hv)
        rows[-1]["description"] = ("Late vendor invoice for April field-sales commission overage — "
                                   "posted in May (prior-period accrual)")
    if config.restatement_enabled:
        _emit(rows, config.restatement_posting_date, config.restatement_doc_date,
              hero.cost_center, hero.cost_center_description, hero.gl_account,
              config.restatement_amount, hv)
        rows[-1]["description"] = ("Post-close true-up for May field-sales commissions — "
                                   "posted after close (period restatement)")

    return pd.DataFrame(rows)[_OUT_COLS]


def _month_end(month_start: dt.date) -> dt.date:
    if month_start.month == 12:
        nxt = month_start.replace(year=month_start.year + 1, month=1)
    else:
        nxt = month_start.replace(month=month_start.month + 1)
    return nxt - dt.timedelta(days=1)
