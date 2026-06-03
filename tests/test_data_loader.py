"""Tests for the data loader (Build Sequence §19 step 3).

The loader is the single I/O owner: it reads raw → **gates** (runs the ingestion
validator and halts loudly on bad data) → types/normalizes → returns. These tests pin
the three behaviours that matter:

  1. clean data loads, with the right dtypes and string-native dimension keys
  2. every kind of bad data halts at the gate (a ``ValueError``/``PipelineHalt``)
  3. a missing file / bad mode fails loud and located, before any analysis

Each test builds an isolated environment under ``tmp_path`` (its own config dir + a
date-named snapshot dir seeded from a fixture), so nothing touches the repo's real data.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.analytics.data_loader import load_data  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "snapshots"
REAL_CONFIG = REPO_ROOT / "config"
CONFIG_TABLES = ("gl_mapping", "cogs_config", "retention_config")
BAD_FIXTURES = sorted(p.name for p in FIXTURES.iterdir()
                      if p.is_dir() and p.name.startswith("bad_"))
AS_OF = "2024-05-22"  # the fixtures' contract date (matches the validator's default)


def _make_env(tmp_path: Path, fixture: str | None, *, mode: str = "snapshot",
              snapshot_date: str = AS_OF, seed_feeds: bool = True) -> Path:
    """Build an isolated config + snapshot tree and return the system_config path.

    Copies the real config tables (tiny) and, when ``seed_feeds``, the five feed CSVs
    from ``fixture`` into a date-named snapshot dir so ``snapshot_date`` parses as a real
    date for the validator's as-of cutoff.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for t in CONFIG_TABLES:
        shutil.copy(REAL_CONFIG / f"{t}.csv", cfg_dir / f"{t}.csv")

    snap_dir = tmp_path / "snapshots" / snapshot_date
    snap_dir.mkdir(parents=True)
    if seed_feeds and fixture is not None:
        for csv in (FIXTURES / fixture).glob("*.csv"):
            shutil.copy(csv, snap_dir / csv.name)

    (tmp_path / "live").mkdir(exist_ok=True)
    cfg = {
        "data_mode": mode,
        "snapshot_date": snapshot_date,
        "snapshot_path": str(tmp_path / "snapshots") + "/",
        "live_data_path": str(tmp_path / "live") + "/",
    }
    cfg_path = cfg_dir / "system_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    return cfg_path


# ---------------------------------------------------------------------------
# 1. Clean data loads, typed, with string-native dimension keys
# ---------------------------------------------------------------------------
def test_clean_loads_with_expected_keys_and_types(tmp_path):
    data = load_data(_make_env(tmp_path, "clean"))
    assert set(data) == {"sales", "conversions", "gl_actuals", "reference_data",
                         "operational_notes", "gl_mapping", "cogs_config",
                         "retention_config"}
    conv = data["conversions"]
    # dates are datetime, numerics are numeric, key is int
    assert conv["conversion_date"].dtype.kind == "M"
    assert data["gl_actuals"]["posting_date"].dtype.kind == "M"
    assert conv["customer_key"].dtype == "int64"
    assert conv["price_per_unit"].dtype.kind == "f"          # nullable float
    assert data["cogs_config"]["cogs_per_unit"].dtype.kind == "f"


def test_dimension_keys_stay_strings_for_join_safety(tmp_path):
    """contract_term_months must be a string ('12' / '') — never 12.0 / 'nan' — so
    fact↔reference↔config joins on the dimension tuple can't drift on dtype."""
    data = load_data(_make_env(tmp_path, "clean"))
    conv = data["conversions"]
    assert conv["contract_term_months"].dtype.kind not in "fi"  # not numeric
    m2m = conv.loc[conv["product_type"] == "Month_to_Month", "contract_term_months"]
    assert (m2m == "").all()
    # the dimension tuples line up byte-for-byte with the config leaves (no coercion)
    dims = ["entity", "region", "service_territory", "segment", "product_type",
            "contract_term_months", "customer_size_tier", "customer_class"]
    conv_tuples = set(conv[dims].itertuples(index=False, name=None))
    cogs_tuples = set(data["cogs_config"][dims].itertuples(index=False, name=None))
    assert conv_tuples <= cogs_tuples


def test_nullable_price_preserved_as_nan(tmp_path):
    # Blank one price_per_unit in the seeded feed (the real data has whole price-less
    # segments, e.g. Online_Partner). A null nullable field must read as NaN — not be
    # dropped, not coerced to 0, and not halt the gate (price_per_unit is nullable).
    cfg_path = _make_env(tmp_path, "clean")
    conv_csv = tmp_path / "snapshots" / AS_OF / "conversions.csv"
    df = pd.read_csv(conv_csv, dtype=str, keep_default_na=False)
    df.loc[0, "price_per_unit"] = ""
    df.to_csv(conv_csv, index=False)

    price = load_data(cfg_path)["conversions"]["price_per_unit"]
    assert price.dtype.kind == "f"
    assert pd.isna(price.iloc[0])


# ---------------------------------------------------------------------------
# 2. The gate: every bad fixture halts loudly, before typing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fixture", BAD_FIXTURES)
def test_bad_data_halts_at_gate(tmp_path, fixture):
    with pytest.raises(ValueError):  # PipelineHalt subclasses ValueError
        load_data(_make_env(tmp_path, fixture))


def test_halt_message_names_file_and_field(tmp_path):
    # The negative-amount fixture should name both gl_actuals.csv and amount.
    with pytest.raises(ValueError) as exc:
        load_data(_make_env(tmp_path, "bad_negative_amount"))
    msg = str(exc.value)
    assert "gl_actuals.csv" in msg and "amount" in msg


# ---------------------------------------------------------------------------
# 3. Missing file / bad mode: loud and located, before any analysis
# ---------------------------------------------------------------------------
def test_missing_file_raises_located_filenotfound(tmp_path):
    cfg_path = _make_env(tmp_path, "clean")
    (tmp_path / "snapshots" / AS_OF / "sales.csv").unlink()
    with pytest.raises(FileNotFoundError) as exc:
        load_data(cfg_path)
    msg = str(exc.value)
    assert "sales.csv" in msg and AS_OF in msg and "snapshot" in msg


def test_live_mode_missing_dir_raises_with_mode(tmp_path):
    cfg_path = _make_env(tmp_path, None, mode="live", seed_feeds=False)
    with pytest.raises(FileNotFoundError) as exc:
        load_data(cfg_path)
    assert "mode='live'" in str(exc.value)


def test_unknown_data_mode_raises(tmp_path):
    cfg_path = _make_env(tmp_path, "clean", mode="warp_drive")
    with pytest.raises(ValueError, match="data_mode"):
        load_data(cfg_path)
