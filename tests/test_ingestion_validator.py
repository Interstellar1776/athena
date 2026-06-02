"""Tests for the ingestion validator (Build Sequence §19 step 2).

Covers the three things that matter for a halt-gate:
  1. Clean pass-through — real snapshot and the clean fixture proceed.
  2. Bad data halts loudly — every committed bad_* fixture raises PipelineHalt, and
     for the *right* reason (asserted on the failing check name, so a fixture tripping
     the wrong rule is caught).
  3. The DAG behaves — a broken schema *skips* its dependent join checks (never
     false-passes), order is deterministic, and independent faults all surface at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.validation.ingestion_validator import (  # noqa: E402
    PipelineHalt, Severity, Status,
    build_dag, load_contracts, read_snapshot_frames,
    validate_ingestion, validate_snapshot_dir, _toposort,
)
from tests.fixtures.build_fixtures import EXPECTED_FAILURE  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "snapshots"
REAL_SNAPSHOT = REPO_ROOT / "data" / "snapshots" / "2024-05-22"


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


# --- 1. Clean pass-through -------------------------------------------------
def test_real_snapshot_passes(contracts):
    report = validate_snapshot_dir(REAL_SNAPSHOT, contracts)
    assert report.ok, report.render()
    assert report.failed_checks() == []


def test_clean_fixture_passes(contracts):
    report = validate_snapshot_dir(FIXTURES / "clean", contracts)
    assert report.ok, report.render()


def test_clean_fixture_raise_is_noop(contracts):
    # raise_if_failed returns the report instead of raising when clean.
    report = validate_snapshot_dir(FIXTURES / "clean", contracts)
    assert report.raise_if_failed() is report


# --- 2. Bad data halts loudly, for the right reason ------------------------
@pytest.mark.parametrize("fixture_name,expected_check", sorted(EXPECTED_FAILURE.items()))
def test_bad_fixture_halts(contracts, fixture_name, expected_check):
    report = validate_snapshot_dir(FIXTURES / fixture_name, contracts)
    assert not report.ok, f"{fixture_name} should not pass"
    assert report.status_of(expected_check) == Status.FAILED, (
        f"{fixture_name} expected {expected_check} to FAIL\n{report.render()}")
    with pytest.raises(PipelineHalt):
        report.raise_if_failed()


def test_halt_message_is_descriptive(contracts):
    report = validate_snapshot_dir(FIXTURES / "bad_negative_amount", contracts)
    with pytest.raises(PipelineHalt) as exc:
        report.raise_if_failed()
    msg = str(exc.value)
    assert "gl_actuals" in msg and "negative" in msg  # plain-language "what and why"


# --- 3. DAG behaviour ------------------------------------------------------
def test_schema_failure_skips_dependent_checks(contracts):
    # A missing column on sales fails schema:sales, which must SKIP (not pass) the
    # downstream content + join checks that read sales.
    report = validate_snapshot_dir(FIXTURES / "bad_missing_column", contracts)
    assert report.status_of("schema:sales") == Status.FAILED
    assert report.status_of("content:sales") == Status.SKIPPED
    assert report.status_of("dim_tuples_known") == Status.SKIPPED
    assert report.status_of("keys_unique") == Status.SKIPPED
    # A feed unaffected by the break still runs.
    assert report.status_of("content:gl_actuals") == Status.PASSED


def test_independent_faults_all_reported(contracts):
    # Two unrelated faults in one run → both checks fail in a single pass.
    frames = read_snapshot_frames(FIXTURES / "clean")
    frames["gl_actuals"].loc[0, "amount"] = "-1"
    frames["sales"].loc[0, "segment"] = "Carrier_Pigeon"
    report = validate_ingestion(frames, contracts)
    assert report.status_of("content:gl_actuals") == Status.FAILED
    assert report.status_of("content:sales") == Status.FAILED


def test_missing_file_halts(contracts):
    frames = read_snapshot_frames(FIXTURES / "clean")
    frames["reference_data"] = None
    report = validate_ingestion(frames, contracts)
    assert report.status_of("present:reference_data") == Status.FAILED
    assert report.status_of("content:reference_data") == Status.SKIPPED
    assert not report.ok


def test_toposort_is_deterministic_and_respects_edges():
    checks = build_dag()
    order1 = [c.name for c in _toposort(checks)]
    order2 = [c.name for c in _toposort(build_dag())]
    assert order1 == order2  # stable
    pos = {name: i for i, name in enumerate(order1)}
    for c in checks:
        for dep in c.depends_on:
            assert pos[dep] < pos[c.name], f"{dep} must run before {c.name}"


def test_plan_coverage_is_warning_not_halt(contracts):
    # Drop the active-period plan rows → plan_covers_acq_units warns, but the run
    # still passes (incomplete-but-sound data is labeled downstream, not halted).
    frames = read_snapshot_frames(FIXTURES / "clean")
    ref = frames["reference_data"]
    active = contracts.active_period
    frames["reference_data"] = ref[~((ref["reference_type"] == "plan")
                                     & ref["date"].str.startswith(active))].reset_index(drop=True)
    report = validate_ingestion(frames, contracts)
    assert report.status_of("plan_covers_acq_units") == Status.WARNED
    assert any(p.severity == Severity.WARNING
               for r in report.results for p in r.problems)
    assert report.ok  # warnings do not halt
