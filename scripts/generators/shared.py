"""shared.py — the single source of truth for synthetic-data generation.

Every generator (gen_sales, gen_gl, gen_reference, gen_notes) draws its
dimensions, dates, economics, and randomness from this module and nowhere else.
That is what guarantees the four tables join cleanly on entity/segment/date and
on cost_center -> entity/segment. No generator may invent a dimension value.

The module is organized as:
  1. Determinism      — seed + per-stream RNG factory
  2. Calendar         — history-depth selector, active period, snapshot dates
  3. Dimensions       — entity / segment / product-type vocabularies
  4. GL accounts      — the canonical chart-of-accounts fragment we post against
  5. SERIES roster    — the curated (entity, segment, product_type) backbone
  6. Derived helpers  — canonical GL mapping, lookups
  7. Noise & shaping  — seasonality, weekday, multiplicative noise, clamps
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------
# A single master seed governs every random draw in the project. Each generator
# asks for its own named RNG stream; streams are derived from (seed, name) so
# adding or reordering a generator never perturbs another generator's numbers,
# and re-runs are byte-identical.

RANDOM_SEED = 42


def make_rng(stream_name: str) -> np.random.Generator:
    """Return an independent, reproducible RNG for a named stream.

    The stream name is hashed (stable across processes — unlike the salted
    built-in hash()) and combined with RANDOM_SEED so each stream is isolated.
    """
    digest = hashlib.sha256(stream_name.encode("utf-8")).digest()
    stream_int = int.from_bytes(digest[:8], "big")
    seed_seq = np.random.SeedSequence([RANDOM_SEED, stream_int])
    return np.random.default_rng(seed_seq)


# ---------------------------------------------------------------------------
# 2. Calendar
# ---------------------------------------------------------------------------
# The active period is May 2024; snapshots cut it cumulatively at four dates.
# HISTORY_MONTHS is the configurable depth selector the user asked for: enough
# trailing history that trailing-3-month and trailing-12-month metrics are real,
# but tunable down to deliberately starve those metrics and exercise fallbacks.

HISTORY_MONTHS = 12  # depth selector — raise/lower to lengthen/shorten history

ACTIVE_PERIOD = "2024-05"
ACTIVE_PERIOD_START = dt.date(2024, 5, 1)
ACTIVE_PERIOD_END = dt.date(2024, 5, 31)
DAYS_IN_ACTIVE_PERIOD = (ACTIVE_PERIOD_END - ACTIVE_PERIOD_START).days + 1

# History runs for HISTORY_MONTHS full months immediately before the active
# period. (Approximate-month arithmetic is fine for synthetic data; we just need
# roughly a year of trailing days.)
HISTORY_START = (ACTIVE_PERIOD_START - dt.timedelta(days=HISTORY_MONTHS * 30)).replace(day=1)

# The cumulative snapshot dates. The first four walk the pre-close demo arc
# (context doc §8); the fifth is the post-close FINAL view of the (now settled)
# May period — taken after books close (period_close_day) and after every
# in-period gain has landed (within the conv-lag SLA), so it shows the closed
# month's true actuals to contrast against the pre-close projections.
SNAPSHOT_DATES = [
    dt.date(2024, 5, 1),   # calm
    dt.date(2024, 5, 8),   # drift
    dt.date(2024, 5, 15),  # building
    dt.date(2024, 5, 22),  # confirmed HIGH (pre-close)
    dt.date(2024, 6, 8),   # final / settled (post-close)
]

# The pre-close arc length (the active-period day the spike/fallout ramps target)
# is the last in-month snapshot, not the post-close one.
LAST_INMONTH_SNAPSHOT = dt.date(2024, 5, 22)


def month_str(d: dt.date) -> str:
    """Period key 'YYYY-MM' for a date."""
    return f"{d.year:04d}-{d.month:02d}"


def daterange(start: dt.date, end: dt.date):
    """Yield every calendar date in [start, end] inclusive."""
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def month_starts(start: dt.date, end: dt.date):
    """Yield the first-of-month date for every month touched by [start, end]."""
    cursor = start.replace(day=1)
    while cursor <= end:
        yield cursor
        # advance to the first of next month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)


# ---------------------------------------------------------------------------
# 3. Dimensions
# ---------------------------------------------------------------------------
# Common, industry-agnostic operational vocabulary, populated with retail-energy
# reference values. Column names stay generic and recognizable; values carry the
# domain flavor (same pattern as an enterprise data mart, where facts arrive
# pre-dimensioned). The hierarchy, top to bottom:
#   entity (market) -> region -> service_territory   (geography)
#   segment                                          (acquisition channel)
#   product_type + contract_term_months             (product)
#   customer_size_tier -> customer_class            (customer; class nests under tier)

# Geography
MARKETS = ["ERCOT", "PJM"]                       # `entity` — ISO / wholesale market
REGIONS = ["North", "South", "West", "East"]     # `region` — sub-market territory
# `service_territory` — the delivery utility (TDU) under a market. Representative,
# not exhaustive; each series carries exactly one.
SERVICE_TERRITORIES = {
    "ERCOT": ["Oncor", "CenterPoint", "AEP_Texas"],
    "PJM": ["PECO", "BGE"],
}

# Acquisition channel
SEGMENTS = ["Paid Search", "Door_to_Door", "Broker", "Affiliate"]

# Product
PRODUCT_TYPES = ["Term", "Month_to_Month"]
CONTRACT_TERMS = [12, 24, 36]   # `contract_term_months` for Term; None for Month_to_Month

# Customer (nested: class only applies to residential)
CUSTOMER_SIZE_TIERS = ["residential", "small_C&I", "large_C&I"]
CUSTOMER_CLASSES = ["single_family", "multi_family"]   # residential only; None for C&I

# customer_key namespacing. Each series numbers its submissions within its own
# block (block = (series_index + 1) * size + local_index, local from 1). Keeps
# keys plain integers while making them stable: retuning one series' volume does
# not renumber any other series. Block size must exceed max submissions/series.
CUSTOMER_KEY_BLOCK_SIZE = 1_000_000

# A submission becomes a conversion (a "gain") iff gen_conversions draws it as
# one; otherwise it is fallout. There is no stored outcome on the sales feed —
# fallout is derived by anti-join (sales with no matching conversion on
# customer_key). See gen_conversions and decisions_log.md.

# The dimension columns every fact/reference row carries, in canonical order.
# Generators emit exactly these (via Series.dims()); the validator compares row
# dimension tuples against the roster on this same ordering.
DIMENSION_COLUMNS = [
    "entity", "region", "service_territory", "segment",
    "product_type", "contract_term_months", "customer_size_tier", "customer_class",
]

# Segments whose operations run on weekdays only (field sales, broker desks).
# Others (digital channels) run every day with only a mild weekend dip.
WEEKDAY_ONLY_SEGMENTS = {"Door_to_Door", "Broker"}


# ---------------------------------------------------------------------------
# 4. GL accounts
# ---------------------------------------------------------------------------
# The minimal chart-of-accounts fragment GL entries post against. Only
# acquisition spend feeds CPA; overhead is mapped out by gl_mapping downstream.

GL_ACCOUNT_ACQUISITION = "6010"   # spend_category -> acquisition_marketing
GL_ACCOUNT_OVERHEAD = "6900"      # spend_category -> overhead

GL_SPEND_CATEGORY = {
    GL_ACCOUNT_ACQUISITION: "acquisition_marketing",
    GL_ACCOUNT_OVERHEAD: "overhead",
}


# ---------------------------------------------------------------------------
# 5. SERIES roster
# ---------------------------------------------------------------------------
# The curated join backbone: one entry per active (entity, segment, product_type)
# combination. Generators iterate SERIES only, so every row they emit carries a
# dimension tuple that exists here — joins cannot break by construction.
#
# Each series also carries its baseline economics. These are the canonical plan
# numbers; the static config CSVs (cogs_config, retention_config, gl_mapping) are
# authored to match, and the orchestrator validates the cost_center mapping.
#
# The roster deliberately spans every data-depth tier so downstream fallback
# logic (real -> trailing-avg -> plan_input) all has data that triggers it:
#   full history · short history · no history · missing revenue · plan-only.


@dataclass(frozen=True)
class Series:
    # --- geography (market -> region -> delivery utility) ---
    entity: str                    # market / ISO (e.g. ERCOT)
    region: str                    # sub-market region (North/South/West/East)
    service_territory: str         # delivery utility / TDU under the market

    # --- channel & product ---
    segment: str                   # acquisition channel
    product_type: str              # Term / Month_to_Month
    contract_term_months: Optional[int]  # 12/24/36 for Term; None for Month_to_Month

    # --- customer (size tier; class nests under residential) ---
    customer_size_tier: str        # residential / small_C&I / large_C&I
    customer_class: Optional[str]  # single_family / multi_family; None for C&I

    # --- GL join key ---
    cost_center: str               # 1:1 with the series; maps via gl_mapping

    # --- baseline daily operations ---
    base_volume_in: float          # daily submissions entering the funnel
    base_conv_rate: float          # fraction that gain/convert (1 - baseline fallout)

    # --- baseline unit economics (the plan numbers) ---
    base_cpa: float                # plan cost per acquisition
    base_cogs_per_unit: float      # plan COGS per unit (mirrors cogs_config)
    base_margin_per_unit: float    # plan margin per unit (used when price is null)
    expected_retention_periods: float  # mirrors retention_config; drives LTV
    base_price_per_unit: Optional[float]  # None -> exercises margin plan fallback

    # --- behavior / config flags ---
    has_forecast: bool             # whether gen_reference emits a forecast row
    cogs_comparison_mode: str      # mirrors cogs_config; spreads across the four modes
    role: str                      # narrative tag (hero/fallout/stable/...)

    # --- per-series history override (None -> global HISTORY_START) ---
    history_start: Optional[dt.date] = None

    # --- slow trailing-CPA drift across history (unit-economics compression) ---
    # Fractional rise in cost-per-conversion from history_start to active period.
    cpa_history_drift: float = 0.0

    def dims(self) -> dict:
        """The dimension columns this series stamps onto every row it produces.
        contract_term_months is stringified ('12'/None) so it round-trips through
        CSV cleanly (no 12.0 float artifacts) and key comparison stays string-pure."""
        return {
            "entity": self.entity,
            "region": self.region,
            "service_territory": self.service_territory,
            "segment": self.segment,
            "product_type": self.product_type,
            "contract_term_months": (str(self.contract_term_months)
                                     if self.contract_term_months is not None else None),
            "customer_size_tier": self.customer_size_tier,
            "customer_class": self.customer_class,
        }

    @property
    def key(self) -> tuple:
        """Full dimension tuple — the join identity. Fact rows must match exactly
        one series on this tuple (validated in the orchestrator)."""
        return tuple(self.dims()[c] for c in DIMENSION_COLUMNS)

    @property
    def effective_history_start(self) -> dt.date:
        return self.history_start or HISTORY_START

    @property
    def plan_ltv(self) -> float:
        """Plan LTV = plan margin per unit x expected retention periods."""
        return round(self.base_margin_per_unit * self.expected_retention_periods, 2)


SERIES: list[Series] = [
    # 1) HERO — Paid Search is the expensive channel: its CPA is already a high
    #    share of LTV (unit economics compressing) AND it spikes hard in May.
    #    Drives the CPA-spike HIGH alert and the CPA-vs-LTV compression alert.
    Series(
        entity="ERCOT", region="North", service_territory="Oncor",
        segment="Paid Search", product_type="Term", contract_term_months=24,
        customer_size_tier="residential", customer_class="single_family",
        cost_center="CC-ONCOR-PS",
        base_volume_in=55, base_conv_rate=0.86,
        base_cpa=118.0, base_cogs_per_unit=40.0, base_margin_per_unit=37.5,
        expected_retention_periods=4.0, base_price_per_unit=77.5,
        has_forecast=True, cogs_comparison_mode="hybrid", role="hero",
        cpa_history_drift=0.10,
    ),
    # 2) Stable control — well-behaved across the whole arc; proves no crying wolf.
    Series(
        entity="ERCOT", region="North", service_territory="Oncor",
        segment="Door_to_Door", product_type="Month_to_Month", contract_term_months=None,
        customer_size_tier="residential", customer_class="single_family",
        cost_center="CC-ONCOR-DD",
        base_volume_in=40, base_conv_rate=0.88,
        base_cpa=95.0, base_cogs_per_unit=35.0, base_margin_per_unit=35.0,
        expected_retention_periods=8.0, base_price_per_unit=70.0,
        has_forecast=False, cogs_comparison_mode="linear_trend", role="stable",
    ),
    # 3) Stable + forecast — carries a forecast row that diverges from plan to
    #    exercise the plan-vs-forecast-gap alert.
    Series(
        entity="ERCOT", region="South", service_territory="CenterPoint",
        segment="Paid Search", product_type="Term", contract_term_months=12,
        customer_size_tier="residential", customer_class="multi_family",
        cost_center="CC-CENTERPOINT-PS",
        base_volume_in=48, base_conv_rate=0.85,
        base_cpa=110.0, base_cogs_per_unit=40.0, base_margin_per_unit=36.0,
        expected_retention_periods=5.0, base_price_per_unit=76.0,
        has_forecast=True, cogs_comparison_mode="hybrid", role="stable",
    ),
    # 4) FALLOUT — conversion rate degrades mid-period so fell_out / submissions
    #    climbs over threshold. Drives the fallout-rate MEDIUM alert.
    Series(
        entity="ERCOT", region="South", service_territory="CenterPoint",
        segment="Door_to_Door", product_type="Month_to_Month", contract_term_months=None,
        customer_size_tier="residential", customer_class="multi_family",
        cost_center="CC-CENTERPOINT-DD",
        base_volume_in=60, base_conv_rate=0.88,
        base_cpa=88.0, base_cogs_per_unit=34.0, base_margin_per_unit=34.0,
        expected_retention_periods=8.0, base_price_per_unit=68.0,
        has_forecast=False, cogs_comparison_mode="prior_year_same_period", role="fallout",
    ),
    # 5) Stable, plan-only — no forecast; prior_year_same_period COGS mode. First
    #    small-C&I series (no customer_class — class is residential-only).
    Series(
        entity="PJM", region="East", service_territory="PECO",
        segment="Broker", product_type="Term", contract_term_months=36,
        customer_size_tier="small_C&I", customer_class=None,
        cost_center="CC-PECO-BR",
        base_volume_in=35, base_conv_rate=0.86,
        base_cpa=130.0, base_cogs_per_unit=45.0, base_margin_per_unit=45.0,
        expected_retention_periods=6.0, base_price_per_unit=90.0,
        has_forecast=False, cogs_comparison_mode="prior_year_same_period", role="stable",
    ),
    # 6) Missing price — price_per_unit is null, so margin must fall back to the
    #    plan margin input. Otherwise well-behaved.
    Series(
        entity="PJM", region="East", service_territory="PECO",
        segment="Affiliate", product_type="Month_to_Month", contract_term_months=None,
        customer_size_tier="residential", customer_class="single_family",
        cost_center="CC-PECO-AF",
        base_volume_in=45, base_conv_rate=0.83,
        base_cpa=70.0, base_cogs_per_unit=32.0, base_margin_per_unit=28.0,
        expected_retention_periods=7.0, base_price_per_unit=None,
        has_forecast=False, cogs_comparison_mode="linear_trend", role="missing_revenue",
    ),
    # 7) Short history — launched ~2 months before the active period, so it has
    #    < 3 months of history and trailing-3-month CPA must fall back.
    Series(
        entity="ERCOT", region="West", service_territory="AEP_Texas",
        segment="Affiliate", product_type="Term", contract_term_months=12,
        customer_size_tier="residential", customer_class="single_family",
        cost_center="CC-AEPTX-AF",
        base_volume_in=30, base_conv_rate=0.84,
        base_cpa=85.0, base_cogs_per_unit=30.0, base_margin_per_unit=30.0,
        expected_retention_periods=5.0, base_price_per_unit=60.0,
        has_forecast=False, cogs_comparison_mode="plan_vs_actual", role="short_history",
        history_start=dt.date(2024, 3, 1),
    ),
    # 8) Brand-new region — launches mid-active-period (May 15). Has plan rows but
    #    no sales/conversions/GL before launch, so every metric falls back to
    #    plan_input in the first two snapshots. Exercises the first-run path.
    #    Also a large-C&I series (no customer_class).
    Series(
        entity="ERCOT", region="West", service_territory="AEP_Texas",
        segment="Broker", product_type="Term", contract_term_months=24,
        customer_size_tier="large_C&I", customer_class=None,
        cost_center="CC-AEPTX-BR",
        base_volume_in=25, base_conv_rate=0.82,
        base_cpa=125.0, base_cogs_per_unit=44.0, base_margin_per_unit=44.0,
        expected_retention_periods=6.0, base_price_per_unit=88.0,
        has_forecast=False, cogs_comparison_mode="plan_vs_actual", role="new",
        history_start=dt.date(2024, 5, 15),
    ),
    # 9) Stable large-C&I — bigger deals, fewer of them; second market (PJM/BGE).
    #    Rounds out large_C&I and the BGE territory.
    Series(
        entity="PJM", region="East", service_territory="BGE",
        segment="Broker", product_type="Term", contract_term_months=36,
        customer_size_tier="large_C&I", customer_class=None,
        cost_center="CC-BGE-BR",
        base_volume_in=20, base_conv_rate=0.87,
        base_cpa=140.0, base_cogs_per_unit=46.0, base_margin_per_unit=48.0,
        expected_retention_periods=6.0, base_price_per_unit=94.0,
        has_forecast=False, cogs_comparison_mode="prior_year_same_period", role="stable",
    ),
    # 10) Stable multi-family residential — rounds out multi_family on a digital
    #     channel; well-behaved.
    Series(
        entity="ERCOT", region="North", service_territory="Oncor",
        segment="Affiliate", product_type="Month_to_Month", contract_term_months=None,
        customer_size_tier="residential", customer_class="multi_family",
        cost_center="CC-ONCOR-AF",
        base_volume_in=50, base_conv_rate=0.85,
        base_cpa=72.0, base_cogs_per_unit=31.0, base_margin_per_unit=27.0,
        expected_retention_periods=7.0, base_price_per_unit=58.0,
        has_forecast=False, cogs_comparison_mode="linear_trend", role="stable",
    ),
]


# ---------------------------------------------------------------------------
# 6. Derived helpers
# ---------------------------------------------------------------------------
# Lookups and the canonical GL mapping derived from SERIES. The authored
# config/gl_mapping.csv must equal canonical_gl_mapping_rows(); the orchestrator
# asserts this so the static table can never silently drift from the roster.

def _is_missing(v) -> bool:
    """True for None or NaN — used to normalize nullable dimension cells without
    importing pandas into this module."""
    return v is None or (isinstance(v, float) and v != v)


def normalize_dim_tuple(values) -> tuple:
    """Coerce a row's dimension values (any order matching DIMENSION_COLUMNS) to
    the canonical key form: missing -> None, everything else -> str. Lets the
    validator compare fact rows (which may carry NaN/float cells after a CSV
    round-trip) against Series.key on equal footing."""
    return tuple(None if _is_missing(v) else str(v) for v in values)


def series_by_cost_center(cost_center: str) -> Optional[Series]:
    for s in SERIES:
        if s.cost_center == cost_center:
            return s
    return None


def canonical_gl_mapping_rows() -> list[dict]:
    """The (cost_center, gl_account) -> entity/region/segment/spend_category rows
    that config/gl_mapping.csv must contain. Each series posts to both an
    acquisition and an overhead account."""
    rows = []
    for s in SERIES:
        for account in (GL_ACCOUNT_ACQUISITION, GL_ACCOUNT_OVERHEAD):
            rows.append({
                "cost_center": s.cost_center,
                "gl_account": account,
                "entity": s.entity,
                "region": s.region,
                "segment": s.segment,
                "spend_category": GL_SPEND_CATEGORY[account],
            })
    return rows


# ---------------------------------------------------------------------------
# 7. Noise & shaping
# ---------------------------------------------------------------------------
# Realistic variation layers multiplicatively on top of the deterministic
# baseline x trend, applied last. Noise is intentionally small so the engineered
# signal stays the dominant, detectable trend — never buried.

def seasonal_factor(d: dt.date) -> float:
    """Mild annual seasonality (~+/-8%), peaking mid-summer — energy demand."""
    # Phase the sine so the peak lands around July (month 7).
    angle = 2.0 * np.pi * (d.month - 7) / 12.0
    return 1.0 + 0.08 * float(np.cos(angle))


def weekday_factor(d: dt.date, segment: str) -> float:
    """Day-of-week shaping. Field/broker channels do not operate on weekends
    (zero activity); digital channels only dip slightly."""
    is_weekend = d.weekday() >= 5  # 5=Sat, 6=Sun
    if segment in WEEKDAY_ONLY_SEGMENTS:
        return 0.0 if is_weekend else 1.0
    return 0.90 if is_weekend else 1.0


def apply_noise(values, rng: np.random.Generator, sd: float):
    """Multiply values by (1 + N(0, sd)). Accepts scalar or array."""
    arr = np.asarray(values, dtype=float)
    factor = 1.0 + rng.normal(0.0, sd, size=arr.shape)
    return arr * factor


def clamp_nonneg(values):
    """Floor at zero — no negative volumes or money."""
    return np.maximum(np.asarray(values, dtype=float), 0.0)


# ---------------------------------------------------------------------------
# 8. Narrative tuning config
# ---------------------------------------------------------------------------
# The knobs that shape the demo narrative WITHOUT hand-editing CSVs. Defaults
# produce the context-doc §8 arc; the orchestrator may override any field
# (e.g. from CLI args). Generators read these to coordinate the cross-table
# story — notably the CPA spike, which is the product of gen_gl ramping spend
# while gen_sales holds conversions roughly flat.
#
# Calibration note: magnitudes target the thresholds the Phase-3 risk classifier
# will use (CPA spike HIGH ~ +20% vs plan; fallout MEDIUM ~ 18%; CPA-vs-LTV
# compression ~ 80% of LTV). Those thresholds are authored for real in Phase 3;
# kept here as the alignment reference so the two never silently diverge.

@dataclass
class NarrativeConfig:
    # CPA spike (hero series, active period) — cumulative CPA-vs-plan spend-side
    # uplift by the day-22 confirmed snapshot. The conversion-date lag inflates the
    # CPA actually *observed* in a cut by a few points (gains lag the spend), so the
    # observed confirmed-snapshot variance lands a bit above this; the default keeps
    # the confirmed snapshot comfortably above the +20% HIGH line while letting the
    # May-15 "building" snapshot sit in approaching-HIGH (MEDIUM) territory.
    cpa_spike_magnitude: float = 0.20

    # Fallout (fallout series) — DAILY fallout rate the ramp reaches by the
    # confirmed (day-22) snapshot. Cumulative month-to-date fallout lags the
    # daily rate, so this is set high enough that MTD fallout visibly climbs.
    # The daily fallout rate (1 - conv_rate) is the per-submission probability of
    # an outcome of "fell_out" rather than "gained".
    fallout_target_rate: float = 0.30
    fallout_start_day: int = 1

    # Sale -> conversion lag. A converting submission gains this many days after
    # its sale_date; the lag is drawn per submission so a gain lands in the
    # snapshot on/after its conversion_date (modeling reporting lag). Clamped to
    # [conv_lag_min_days, conv_lag_max_days]. Kept short relative to the 7-day
    # snapshot cadence so gains (and therefore the derived fallout) resolve within
    # the demo window — a too-long lag leaves every recent sale unresolved and
    # masks both the CPA climb and the fallout ramp.
    conv_lag_mean_days: float = 3.0
    conv_lag_min_days: int = 1
    conv_lag_max_days: int = 7

    # Plan-vs-forecast gap (has_forecast series) — forecast CPA divergence + issue date
    forecast_divergence: float = 0.12
    forecast_issue_date: dt.date = dt.date(2024, 5, 10)

    # Late / accrued GL invoice — prior-period document_date, current-period posting
    late_invoice_enabled: bool = True
    late_invoice_amount: float = 9800.0
    late_invoice_doc_date: dt.date = dt.date(2024, 4, 27)
    late_invoice_posting_date: dt.date = dt.date(2024, 5, 20)
    late_invoice_cost_center: str = "CC-ONCOR-PS"

    # Global multiplicative noise level
    noise_sd: float = 0.04
