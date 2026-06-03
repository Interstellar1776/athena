#!/usr/bin/env python3
"""data_loader.py — the single mouth of the analytics pipeline (load · gate · clean).

Build Sequence §19 step 3, first analytics sub-module (context doc §15). It is the
**single I/O owner**: everything downstream gets its data through this module and never
touches disk or worries about source mode.

The two-mode contract (context doc §8 [LOCKED]):

* **snapshot mode** (dev + demo) reads a cumulative dated cut from
  ``{snapshot_path}/{snapshot_date}/`` — the snapshot date is set in
  ``system_config.yaml``.
* **live mode** (production) reads the same five file names from ``{live_data_path}/``;
  ``snapshot_date`` is irrelevant and ignored.

Both modes return the **identical structure**: a flat dict of dataframes keyed by table
name. Downstream code is written once and never branches on mode — switching dev↔prod
is a one-line config change, not a code change.

What this module does, in order:

1. **Read the raw files once, as strings** (the validator's byte-exact policy).
   A missing file raises ``FileNotFoundError`` naming the exact path + mode + snapshot
   date — the loud, actionable failure the original loader contract requires.
2. **Gate before anything else.** It runs ``ingestion_validator`` on the raw frames and
   halts loudly (``PipelineHalt``) if the data is structurally bad — *check the raw
   files first, then load* (CLAUDE.md: never analyze unvalidated data). The validator
   needs raw strings to detect type errors a coercing read would swallow, which is
   exactly why typing happens only *after* the gate.
3. **Type + normalize (the cleaner, folded in).** With the gate passed, null/dedup/
   bad-record detection is already done, so the residual cleaner job is normalization:
   parse dates to ``datetime64``, coerce numeric columns, and keep the dimension keys
   as canonical strings (``contract_term_months`` stays ``"12"`` / ``""``, never ``12.0``
   / ``"nan"``) so fact↔reference↔config joins downstream can't drift on dtype.

Config tables (``gl_mapping``, ``cogs_config``, ``retention_config``) are read **once**
here and reused both to build the validation contract and to be returned typed — no
double read. ``_load_config_tables`` is the seam a future web app would cache or inject.

CLI:
    python -m app.analytics.data_loader
    # gates + loads the configured snapshot, then prints {table: shape}.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import yaml

# The validator is the gate; the loader reuses its raw-read policy and contract builder
# rather than re-implementing them (analytics may depend on the validation gate; the
# validator never imports analytics).
from app.validation.ingestion_validator import (
    FEEDS,
    _as_date,
    _read_csv,
    build_contracts,
    validate_ingestion,
)

# ---------------------------------------------------------------------------
# Repo layout — resolved relative to this file so the module is location-stable
# (mirrors ingestion_validator's APP_DIR/REPO_ROOT convention).
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_SYSTEM_CONFIG = DEFAULT_CONFIG_DIR / "system_config.yaml"

# ===========================================================================
# 1. The typing contract — which columns are dates / ints / floats per table
# ===========================================================================
# Everything not listed here stays a string (the dimension keys and free text), which
# is deliberate: dimension columns are join keys and must compare byte-for-byte against
# the config/reference frames, so they are never coerced to numbers.
FEED_DATE_COLUMNS: dict[str, list[str]] = {
    "sales": ["sale_date"],
    "conversions": ["sale_date", "conversion_date"],
    "gl_actuals": ["posting_date", "document_date"],
    "reference_data": ["date"],
    "operational_notes": ["date"],
}

FEED_INT_COLUMNS: dict[str, list[str]] = {
    "sales": ["customer_key"],
    "conversions": ["customer_key"],
    "reference_data": ["volume_in_ref", "volume_converted_ref"],
}

FEED_FLOAT_COLUMNS: dict[str, list[str]] = {
    "conversions": ["price_per_unit"],                       # nullable → NaN
    "gl_actuals": ["amount"],
    "reference_data": ["cost_ref", "cpa_ref", "cogs_ref", "ltv_ref", "margin_ref"],
}

# The standing config / reference tables, always read from config/ (both modes). Their
# numeric inputs are typed for the metrics layer; effective_date is left as a string for
# now (out of scope for this loader's date contract).
CONFIG_FLOAT_COLUMNS: dict[str, list[str]] = {
    "gl_mapping": [],
    "cogs_config": ["cogs_per_unit"],
    "retention_config": ["expected_retention_periods"],
}
CONFIG_TABLES: tuple[str, ...] = tuple(CONFIG_FLOAT_COLUMNS)

# The ISO date format the data dictionary fixes for every feed; parsing against it
# explicitly (rather than inferring) is what makes errors="raise" meaningful.
DATE_FORMAT = "%Y-%m-%d"


# ===========================================================================
# 2. Source resolution — the one place mode actually matters
# ===========================================================================
def _resolve_source_dir(cfg: dict) -> tuple[Path, str, object]:
    """Resolve the directory the five feeds are read from, per ``data_mode``.

    Returns ``(source_dir, mode, snapshot_date)`` — the latter two ride along so error
    messages can name exactly what was requested. Config paths may be relative; they
    resolve against the repo root.
    """
    mode = cfg.get("data_mode")

    if mode == "snapshot":
        snapshot_date = cfg["snapshot_date"]
        return _abs(cfg["snapshot_path"]) / str(snapshot_date), mode, snapshot_date

    if mode == "live":
        # Production: snapshot_date is meaningless here and intentionally ignored.
        return _abs(cfg["live_data_path"]), mode, None

    # Anything else is a misconfiguration, not a data problem — fail loudly.
    raise ValueError(
        f"system_config.yaml: data_mode must be 'snapshot' or 'live', got {mode!r}"
    )


def _abs(path_str: str) -> Path:
    """Resolve a config path: absolute as-is, relative against the repo root."""
    p = Path(path_str)
    return p if p.is_absolute() else REPO_ROOT / p


# ===========================================================================
# 3. Raw read — existence-checked, string-typed (feeds the gate)
# ===========================================================================
def _read_raw(path: Path, *, mode: str, snapshot_date: object) -> pd.DataFrame:
    """Read one CSV as strings (blanks kept as ``""``), or raise a located error.

    A missing file is the one structural failure the loader owns directly (the gate
    handles bad *content*): ``FileNotFoundError`` names the exact path, the mode, and
    the requested snapshot date so the message points straight at the fix.
    """
    if not path.exists():
        ctx = (f"mode={mode!r}, snapshot_date={str(snapshot_date)!r}"
               if mode == "snapshot" else f"mode={mode!r}")
        raise FileNotFoundError(f"Required data file not found: {path} ({ctx})")
    return _read_csv(path)


def _load_config_tables(config_dir: Path, *, mode: str,
                        snapshot_date: object) -> dict[str, pd.DataFrame]:
    """Read the three config tables once, as strings. This is the single config read in
    the pipeline; a future web layer would cache or inject the result here."""
    return {t: _read_raw(config_dir / f"{t}.csv", mode=mode, snapshot_date=snapshot_date)
            for t in CONFIG_TABLES}


# ===========================================================================
# 4. Typing / normalization — the cleaner, applied only after the gate passes
# ===========================================================================
def _coerce_dates(df: pd.DataFrame, cols: list[str]) -> None:
    """Parse ISO date columns to datetime64. ``errors="raise"`` keeps a bad date loud
    rather than silently coercing to NaT — though the gate has already vetted them, so
    a raise here would signal a logic bug, not dirty input."""
    for col in cols:
        df[col] = pd.to_datetime(df[col], format=DATE_FORMAT, errors="raise")


def _coerce_numeric(df: pd.DataFrame, int_cols: list[str], float_cols: list[str]) -> None:
    """Coerce numeric columns; blanks ("") in nullable float fields become NaN. Int
    columns are non-nullable by contract, so they parse cleanly to int64."""
    for col in int_cols:
        df[col] = pd.to_numeric(df[col], errors="raise").astype("int64")
    for col in float_cols:
        df[col] = pd.to_numeric(df[col].mask(df[col] == "", other=pd.NA), errors="raise")


def _clean_feed(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Type a single feed: dates + numerics. Dimension keys stay as canonical strings."""
    _coerce_dates(df, FEED_DATE_COLUMNS.get(name, []))
    _coerce_numeric(df, FEED_INT_COLUMNS.get(name, []), FEED_FLOAT_COLUMNS.get(name, []))
    return df


def _clean_config(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Type a config table: numeric inputs only (keys/labels/dates stay strings)."""
    _coerce_numeric(df, [], CONFIG_FLOAT_COLUMNS.get(name, []))
    return df


# ===========================================================================
# 5. Public entry point — read raw, gate, then type and return
# ===========================================================================
def load_data(config_path: Path = DEFAULT_SYSTEM_CONFIG) -> dict[str, pd.DataFrame]:
    """Load the five feeds (per ``data_mode``) plus the three config tables.

    Reads raw → validates (halts loudly on bad data) → types/normalizes → returns. The
    return is a flat dict keyed by table name, identical in snapshot or live mode::

        {
            "sales": df, "conversions": df, "gl_actuals": df,
            "reference_data": df, "operational_notes": df,   # feeds (mode-dependent source)
            "gl_mapping": df, "cogs_config": df, "retention_config": df,  # config (always config/)
        }

    Raises ``FileNotFoundError`` if any file is missing, or ``PipelineHalt`` (a
    ``ValueError``) if the data fails ingestion validation.
    """
    config_path = Path(config_path)
    config_dir = config_path.parent
    cfg = yaml.safe_load(config_path.read_text())

    source_dir, mode, snapshot_date = _resolve_source_dir(cfg)

    # --- 1. Read raw (strings), config once + the five feeds from the resolved dir.
    config_raw = _load_config_tables(config_dir, mode=mode, snapshot_date=snapshot_date)
    feeds_raw = {feed: _read_raw(source_dir / f"{feed}.csv",
                                 mode=mode, snapshot_date=snapshot_date)
                 for feed in FEEDS}

    # --- 2. Gate before loading: check the raw files first, halt loudly on bad data.
    # NOTE (live mode, deferred per project owner): the validator's "no future dates"
    # cutoff uses snapshot_date. In live mode there is no snapshot date; this falls back
    # to the configured value as a placeholder until live cutoff semantics are defined.
    as_of: dt.date = _as_date(cfg["snapshot_date"])
    contracts = build_contracts(config_raw["gl_mapping"], config_raw["cogs_config"], as_of)
    validate_ingestion(feeds_raw, contracts, snapshot=str(source_dir)).raise_if_failed()

    # --- 3. Type + normalize now that the data is trusted (the cleaner, folded in).
    data: dict[str, pd.DataFrame] = {f: _clean_feed(f, df) for f, df in feeds_raw.items()}
    data.update({t: _clean_config(t, df) for t, df in config_raw.items()})
    return data


# ===========================================================================
# 6. CLI — manual verification aid (build ethos: eyeball every step)
# ===========================================================================
def main() -> int:
    data = load_data()
    print("Loaded + validated tables (key: rows × cols):")
    for key, df in data.items():
        print(f"  {key:18s} {df.shape[0]:>8,} × {df.shape[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
