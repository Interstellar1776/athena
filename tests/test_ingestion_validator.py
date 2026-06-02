"""Tests for the ingestion validator (Build Sequence §19 step 2).

The gate's job is binary and loud: clean data flows through; bad data raises a
descriptive ``ValueError`` (``PipelineHalt`` is a ``ValueError``) whose message names
**both the file and the field** at fault, so an operator can act on it without a
debugger. Every failure test below asserts on those substrings — not merely that it
raised.

Layout:
  1. clean pass-through — every snapshot date
  2. missing required column
  3. wrong type in a critical field
  4. null in a critical field
  5. negative CPA or amount
  6. unresolvable cost_center (not in gl_mapping)
  7. entity/segment in the facts with no reference_data row
Plus a short section pinning the DAG behaviour (skip-on-broken-prereq, determinism).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.validation.ingestion_validator import (  # noqa: E402
    PipelineHalt, Status,
    build_dag, load_contracts, read_snapshot_frames,
    validate_ingestion, validate_snapshot_dir, _toposort,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "snapshots"
SNAPSHOT_DIRS = sorted(p for p in (REPO_ROOT / "data" / "snapshots").iterdir() if p.is_dir())


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


@pytest.fixture
def clean_frames():
    """A fresh, mutable copy of the clean fixture for per-test fault injection."""
    return read_snapshot_frames(FIXTURES / "clean")


def _halt_message(frames, contracts) -> str:
    """Run the gate and return the ValueError message it halts with."""
    with pytest.raises(ValueError) as exc:
        validate_ingestion(frames, contracts).raise_if_failed()
    return str(exc.value)


# ---------------------------------------------------------------------------
# 1. Clean data passes through — every snapshot date
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("snapshot_dir", SNAPSHOT_DIRS, ids=lambda p: p.name)
def test_clean_snapshot_passes(contracts, snapshot_dir):
    report = validate_snapshot_dir(snapshot_dir, contracts)
    assert report.ok, report.render()
    assert report.failed_checks() == []
    # raise_if_failed is a no-op on clean data (returns the report, does not raise).
    assert report.raise_if_failed() is report


def test_clean_fixture_passes(contracts, clean_frames):
    report = validate_ingestion(clean_frames, contracts)
    assert report.ok, report.render()


# ---------------------------------------------------------------------------
# 2. Missing required column
# ---------------------------------------------------------------------------
def test_missing_required_column(contracts, clean_frames):
    clean_frames["sales"] = clean_frames["sales"].drop(columns=["segment"])
    msg = _halt_message(clean_frames, contracts)
    assert "sales.csv" in msg
    assert "segment" in msg
    assert "missing required column" in msg


# ---------------------------------------------------------------------------
# 3. Wrong type in a critical field
# ---------------------------------------------------------------------------
def test_wrong_type_in_amount(contracts, clean_frames):
    clean_frames["gl_actuals"].loc[0, "amount"] = "not_a_number"
    msg = _halt_message(clean_frames, contracts)
    assert "gl_actuals.csv" in msg
    assert "amount" in msg
    assert "non-numeric" in msg


def test_wrong_type_in_customer_key(contracts, clean_frames):
    clean_frames["sales"].loc[0, "customer_key"] = "ABC123"
    msg = _halt_message(clean_frames, contracts)
    assert "sales.csv" in msg
    assert "customer_key" in msg


# ---------------------------------------------------------------------------
# 4. Null in a critical field
# ---------------------------------------------------------------------------
def test_null_in_critical_field(contracts, clean_frames):
    clean_frames["sales"].loc[0, "entity"] = ""
    msg = _halt_message(clean_frames, contracts)
    assert "sales.csv" in msg
    assert "entity" in msg
    assert "null" in msg.lower()


# ---------------------------------------------------------------------------
# 5. Negative CPA or amount
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("feed,field", [("gl_actuals", "amount"),
                                        ("reference_data", "cpa_ref")])
def test_negative_value_rejected(contracts, clean_frames, feed, field):
    clean_frames[feed].loc[0, field] = "-12.50"
    msg = _halt_message(clean_frames, contracts)
    assert f"{feed}.csv" in msg
    assert field in msg
    assert "negative" in msg


# ---------------------------------------------------------------------------
# 6. Unresolvable cost_center (not in gl_mapping)
# ---------------------------------------------------------------------------
def test_unresolvable_cost_center(contracts, clean_frames):
    clean_frames["gl_actuals"].loc[0, "cost_center"] = "9999"
    msg = _halt_message(clean_frames, contracts)
    assert "gl_actuals.csv" in msg
    assert "cost_center" in msg
    assert "gl_mapping.csv" in msg  # names where the resolution should have come from


# ---------------------------------------------------------------------------
# 7. Entity/segment in the facts with no reference_data row
# ---------------------------------------------------------------------------
def test_facts_without_reference_row(contracts, clean_frames):
    # Drop every reference_data row for the (entity, segment) of the first sale,
    # leaving the facts uncovered by any plan/forecast.
    sales = clean_frames["sales"]
    entity, segment = sales.loc[0, "entity"], sales.loc[0, "segment"]
    ref = clean_frames["reference_data"]
    clean_frames["reference_data"] = ref[~((ref["entity"] == entity)
                                           & (ref["segment"] == segment))].reset_index(drop=True)
    msg = _halt_message(clean_frames, contracts)
    assert "reference_data.csv" in msg
    assert "entity" in msg and "segment" in msg
    assert entity in msg and segment in msg


# ---------------------------------------------------------------------------
# DAG behaviour — the structural guarantees behind the checks above
# ---------------------------------------------------------------------------
def test_broken_schema_skips_dependent_checks(contracts, clean_frames):
    # A missing column fails schema:sales, which must SKIP (not silently pass) the
    # downstream content + join checks that read sales.
    clean_frames["sales"] = clean_frames["sales"].drop(columns=["segment"])
    report = validate_ingestion(clean_frames, contracts)
    assert report.status_of("schema:sales") == Status.FAILED
    assert report.status_of("content:sales") == Status.SKIPPED
    assert report.status_of("dim_tuples_known") == Status.SKIPPED
    assert report.status_of("content:gl_actuals") == Status.PASSED  # unaffected feed runs


def test_independent_faults_all_reported(contracts, clean_frames):
    clean_frames["gl_actuals"].loc[0, "amount"] = "-1"
    clean_frames["sales"].loc[0, "segment"] = "Carrier_Pigeon"
    report = validate_ingestion(clean_frames, contracts)
    assert report.status_of("content:gl_actuals") == Status.FAILED
    assert report.status_of("content:sales") == Status.FAILED


def test_missing_file_halts(contracts, clean_frames):
    clean_frames["reference_data"] = None
    report = validate_ingestion(clean_frames, contracts)
    assert report.status_of("present:reference_data") == Status.FAILED
    assert report.status_of("content:reference_data") == Status.SKIPPED
    assert not report.ok


def test_toposort_is_deterministic_and_respects_edges():
    checks = build_dag()
    order1 = [c.name for c in _toposort(checks)]
    order2 = [c.name for c in _toposort(build_dag())]
    assert order1 == order2
    pos = {name: i for i, name in enumerate(order1)}
    for c in checks:
        for dep in c.depends_on:
            assert pos[dep] < pos[c.name], f"{dep} must run before {c.name}"


def test_pipeline_halt_is_a_value_error(contracts, clean_frames):
    clean_frames["gl_actuals"].loc[0, "amount"] = "-1"
    with pytest.raises(ValueError):  # PipelineHalt subclasses ValueError
        validate_ingestion(clean_frames, contracts).raise_if_failed()
    assert issubclass(PipelineHalt, ValueError)
