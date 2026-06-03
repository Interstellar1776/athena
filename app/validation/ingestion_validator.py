#!/usr/bin/env python3
"""ingestion_validator.py — the loud halt-gate at the mouth of the pipeline.

Build Sequence §19 step 2. Sits between the data loader and the analytics core
(context doc §15 batch flow): it validates *incoming* data for schema, type and
join integrity and **halts the pipeline loudly** on any structural problem, so bad
data never flows downstream (CLAUDE.md non-negotiable; §17 error philosophy).

Design choices that matter:

* **Runtime contract, not generator roster.** The synthetic-data generator validates
  its *output* against its private ``shared.SERIES``. This module is a *runtime* gate:
  its ground truth is the shipped contracts — ``config/gl_mapping.csv`` (valid GL
  combos + acquisition units), ``config/cogs_config.csv`` (the 33 valid leaf dimension
  tuples) and the enums fixed in ``docs/data_dictionary.md``. Nothing here imports
  generator internals, so the same gate works on snapshot *or* live feeds.

* **A DAG of checks.** Each check is a node with explicit dependencies. A check whose
  prerequisite failed is *skipped* (you can't anti-join on a column the schema is
  missing) and reported as such — rather than crashing or silently passing. Every
  independent failure is collected in one pass, so a single run surfaces *all* the
  problems, not just the first.

* **One report, then halt.** Findings aggregate into a ``ValidationReport``. ERROR
  findings halt; WARNING findings are surfaced but pass. The halt message is the full
  human-readable report — *exactly what failed and why*.

CLI:
    python -m app.validation.ingestion_validator <snapshot_dir>
    # exits 0 on clean pass-through, non-zero (with the report) on halt.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sys
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# Repo layout — resolved relative to this file so the module is location-stable.
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_SYSTEM_CONFIG = DEFAULT_CONFIG_DIR / "system_config.yaml"


class PipelineHalt(ValueError):
    """Raised to halt the pipeline loudly with a plain-language reason.

    Subclasses ``ValueError``: a halt is fundamentally "the incoming values are
    wrong", so callers (and tests) can catch the standard exception while the name
    still reads as the loud-failure contract (§17). Mirrors the generator's halt by
    design — but defined here because ``app/`` must never import from ``scripts/``.
    """


# ===========================================================================
# 1. The shipped data contract (sourced from docs/data_dictionary.md)
# ===========================================================================
# The dimension hierarchy every fact / reference row carries, in canonical order.
DIMENSION_COLUMNS = [
    "entity", "region", "service_territory", "segment",
    "product_type", "contract_term_months", "customer_size_tier", "customer_class",
]

# The five ingested feeds and their required columns. A feed missing any of these
# halts at the schema tier; unexpected extra columns are surfaced as a warning.
FEEDS = ("sales", "conversions", "gl_actuals", "reference_data", "operational_notes")

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "sales": ["customer_key", "sale_date", *DIMENSION_COLUMNS],
    "conversions": ["customer_key", "sale_date", "conversion_date",
                    *DIMENSION_COLUMNS, "price_per_unit"],
    "gl_actuals": ["posting_date", "document_date", "cost_center",
                   "cost_center_description", "gl_account", "gl_account_description",
                   "amount", "vendor", "description"],
    "reference_data": ["date", *DIMENSION_COLUMNS, "reference_type",
                       "volume_in_ref", "volume_converted_ref", "cost_ref",
                       "cpa_ref", "cogs_ref", "ltv_ref", "margin_ref"],
    "operational_notes": ["date", "entity", "region", "segment", "note_text", "author"],
}

# Categorical domains. Membership is checked only on non-empty values; nullability
# is a separate rule so the two failure modes report distinctly.
ENUMS: dict[str, set[str]] = {
    "segment": {"Web_Direct", "Door_to_Door", "Telemarketing",
                "Inbound_Call_Center", "Direct_Mail", "Online_Partner"},
    "product_type": {"Term", "Month_to_Month"},
    "customer_size_tier": {"residential", "small_C&I", "large_C&I"},
    "customer_class": {"single_family", "multi_family"},
    "reference_type": {"plan", "forecast"},
}
VALID_CONTRACT_TERMS = {"12", "24", "36"}
NOTE_WILDCARD = "ALL"  # per-level wildcard scope on operational_notes


# ===========================================================================
# 2. Findings, checks, and the report
# ===========================================================================
class Severity(Enum):
    ERROR = "ERROR"      # halts the pipeline
    WARNING = "WARNING"  # surfaced, but passes


class Status(Enum):
    PASSED = "PASS"
    WARNED = "WARN"
    FAILED = "FAIL"
    SKIPPED = "SKIP"


@dataclass
class Problem:
    """One thing wrong, attributed to the check that found it."""
    severity: Severity
    message: str


def _err(msg: str) -> Problem:
    return Problem(Severity.ERROR, msg)


def _warn(msg: str) -> Problem:
    return Problem(Severity.WARNING, msg)


@dataclass
class Check:
    """A node in the validation DAG.

    ``fn`` inspects the frames + contracts and returns the problems it found (empty
    if clean). ``depends_on`` names the checks that must succeed first — a check is
    skipped if any dependency failed or was itself skipped.
    """
    name: str
    depends_on: tuple[str, ...]
    fn: Callable[["Frames", "Contracts"], list[Problem]]


@dataclass
class CheckResult:
    name: str
    status: Status
    problems: list[Problem] = field(default_factory=list)
    skip_reason: str = ""

    @property
    def blocks_dependents(self) -> bool:
        # A failed (ERROR) check or a skipped check blocks anything downstream;
        # a check that only warned does not.
        return self.status in (Status.FAILED, Status.SKIPPED)


@dataclass
class ValidationReport:
    snapshot: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Clean enough to proceed: nothing failed and nothing was skipped.

        WARNING-only checks pass — incomplete-but-structurally-sound data is the
        analytics core's problem to label, not a reason to halt ingestion.
        """
        return not any(r.status in (Status.FAILED, Status.SKIPPED) for r in self.results)

    def status_of(self, check_name: str) -> Optional[Status]:
        for r in self.results:
            if r.name == check_name:
                return r.status
        return None

    def failed_checks(self) -> list[str]:
        return [r.name for r in self.results if r.status == Status.FAILED]

    def render(self) -> str:
        lines = [f"INGESTION VALIDATION REPORT — snapshot: {self.snapshot}",
                 "=" * 70]
        for r in self.results:
            lines.append(f"  [{r.status.value}] {r.name}"
                         + (f"  ({r.skip_reason})" if r.skip_reason else ""))
            for p in r.problems:
                lines.append(f"        - {p.severity.value:7s} {p.message}")
        n_fail = sum(r.status == Status.FAILED for r in self.results)
        n_skip = sum(r.status == Status.SKIPPED for r in self.results)
        n_warn = sum(r.status == Status.WARNED for r in self.results)
        lines.append("-" * 70)
        verdict = "PASS — data may proceed" if self.ok else "HALT — bad data refused"
        lines.append(f"  {verdict}   "
                     f"(failed={n_fail}, skipped={n_skip}, warnings={n_warn})")
        return "\n".join(lines)

    def raise_if_failed(self) -> "ValidationReport":
        """Loud halt: if anything failed/skipped, raise with the full report (§17)."""
        if not self.ok:
            raise PipelineHalt(self.render())
        return self


# ===========================================================================
# 3. Contracts — the runtime ground truth, loaded from config/
# ===========================================================================
Frames = dict[str, Optional[pd.DataFrame]]


@dataclass
class Contracts:
    snapshot_date: dt.date                   # as-of cutoff: no feed row may post-date this
    active_period: str                       # "YYYY-MM" business period under analysis
    gl_combos: set[tuple[str, str, str]]     # (cost_center, gl_account, vendor)
    acq_units: set[tuple[str, str, str]]     # (entity, region, segment), acquisition
    leaf_tuples: set[tuple[str, ...]]        # the 33 valid 8-field dimension tuples
    valid_note_scopes: set[tuple[str, str, str]]  # (entity, region, segment)


def build_contracts(gl_map: pd.DataFrame, cogs: pd.DataFrame,
                    snapshot_date: dt.date) -> Contracts:
    """Assemble the runtime contract from already-loaded config frames.

    Split out from ``load_contracts`` so a caller that has *already* read the config
    tables (e.g. ``data_loader``, the single I/O owner) can build the contract without
    a second disk read. The frames must be string-typed with blanks kept as ``""`` (the
    ``_read_csv`` policy) so the contract keys line up byte-for-byte with how the feeds
    are validated — no float reformatting, no NaN/empty ambiguity."""
    gl_combos = set(gl_map[["cost_center", "gl_account", "vendor"]]
                    .itertuples(index=False, name=None))
    acq = gl_map[gl_map["spend_category"] == "acquisition_marketing"]
    acq_units = set(acq[["entity", "region", "segment"]].itertuples(index=False, name=None))
    leaf_tuples = set(cogs[DIMENSION_COLUMNS].itertuples(index=False, name=None))
    valid_scopes = {(e, r, s) for (e, r, _st, s, *_rest) in leaf_tuples}

    return Contracts(
        snapshot_date=snapshot_date,
        active_period=snapshot_date.strftime("%Y-%m"),
        gl_combos=gl_combos,
        acq_units=acq_units,
        leaf_tuples=leaf_tuples,
        valid_note_scopes=valid_scopes,
    )


def load_contracts(config_dir: Path = DEFAULT_CONFIG_DIR,
                   system_config_path: Path = DEFAULT_SYSTEM_CONFIG) -> Contracts:
    """Read the shipped config tables from disk and build the runtime contract.

    Convenience wrapper over ``build_contracts`` for callers that don't already hold
    the config frames. Everything is read as string with blanks preserved as ``""``."""
    sys_cfg = yaml.safe_load(system_config_path.read_text())
    snapshot_date = _as_date(sys_cfg["snapshot_date"])
    gl_map = _read_csv(config_dir / "gl_mapping.csv")
    cogs = _read_csv(config_dir / "cogs_config.csv")
    return build_contracts(gl_map, cogs, snapshot_date)


# ===========================================================================
# 4. Small shared validation helpers
# ===========================================================================
def _read_csv(path: Path) -> pd.DataFrame:
    """Canonical load: everything string, blanks kept as ``""`` (not NaN).

    Reading as string is deliberate — the validator inspects raw values to *detect*
    type errors a coercing loader would silently swallow."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _as_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _sample(items, n: int = 5) -> str:
    """Render a bounded sample of offending values for a problem message."""
    items = sorted(map(str, items))
    head = ", ".join(items[:n])
    return head + (f" … (+{len(items) - n} more)" if len(items) > n else "")


def _loc(feed: str, *cols: str) -> str:
    """Locate a problem by file + field — every error message names both the file
    (``sales.csv``) and the offending column(s), so a halt points straight at the fix."""
    label = "column" if len(cols) == 1 else "columns"
    fields = ", ".join(f"'{c}'" for c in cols)
    return f"{feed}.csv ({label} {fields})"


def _nonempty_mask(s: pd.Series) -> pd.Series:
    return s.astype(str).str.len() > 0


def _check_nonnull(df, feed, cols) -> list[Problem]:
    out = []
    for c in cols:
        empties = (~_nonempty_mask(df[c])).sum()
        if empties:
            out.append(_err(f"{_loc(feed, c)}: {empties} null/empty value(s) in a non-nullable field"))
    return out


def _check_dates(df, feed, cols, contracts, allow_future) -> list[Problem]:
    out = []
    for c in cols:
        present = df[c][_nonempty_mask(df[c])]
        parsed = pd.to_datetime(present, format="%Y-%m-%d", errors="coerce")
        bad = present[parsed.isna()]
        if len(bad):
            out.append(_err(f"{_loc(feed, c)}: {len(bad)} unparseable date(s) "
                            f"(want YYYY-MM-DD): {_sample(bad.unique())}"))
        if not allow_future:
            future = present[parsed.dt.date > contracts.snapshot_date]
            if len(future):
                out.append(_err(f"{_loc(feed, c)}: {len(future)} date(s) after the "
                                f"snapshot date {contracts.snapshot_date}: "
                                f"{_sample(future.unique())}"))
    return out


def _check_numeric(df, feed, col, *, integer=False, nonneg=False, nullable=False) -> list[Problem]:
    out = []
    present = df[col][_nonempty_mask(df[col])]
    if not nullable and len(present) != len(df):
        # nullability is enforced separately; here we only validate present values
        pass
    nums = pd.to_numeric(present, errors="coerce")
    bad = present[nums.isna()]
    if len(bad):
        out.append(_err(f"{_loc(feed, col)}: {len(bad)} non-numeric value(s): {_sample(bad.unique())}"))
    good = nums.dropna()
    if integer and ((good % 1) != 0).any():
        out.append(_err(f"{_loc(feed, col)}: {((good % 1) != 0).sum()} non-integer value(s)"))
    if nonneg and (good < 0).any():
        out.append(_err(f"{_loc(feed, col)}: {(good < 0).sum()} negative value(s)"))
    return out


def _check_enum(df, feed, col) -> list[Problem]:
    allowed = ENUMS[col]
    present = df[col][_nonempty_mask(df[col])]
    bad = present[~present.isin(allowed)]
    if len(bad):
        return [_err(f"{_loc(feed, col)}: {len(bad)} value(s) outside {sorted(allowed)}: "
                     f"{_sample(bad.unique())}")]
    return []


def _check_dim_conditionals(df, feed) -> list[Problem]:
    """The two cross-field dimension rules from the data dictionary."""
    out = []
    # contract_term_months: present∈{12,24,36} for Term, null for Month_to_Month.
    ct = df["contract_term_months"].astype(str)
    is_m2m = df["product_type"] == "Month_to_Month"
    is_term = df["product_type"] == "Term"
    bad_m2m = (is_m2m & _nonempty_mask(ct)).sum()
    if bad_m2m:
        out.append(_err(f"{_loc(feed, 'contract_term_months')}: {bad_m2m} Month_to_Month "
                        f"row(s) carry a term (must be null)"))
    bad_term = (is_term & ~ct.isin(VALID_CONTRACT_TERMS)).sum()
    if bad_term:
        out.append(_err(f"{_loc(feed, 'contract_term_months')}: {bad_term} Term row(s) "
                        f"without a valid term in {sorted(VALID_CONTRACT_TERMS)}"))
    # customer_class is residential-only.
    bad_class = (_nonempty_mask(df["customer_class"])
                 & (df["customer_size_tier"] != "residential")).sum()
    if bad_class:
        out.append(_err(f"{_loc(feed, 'customer_class')}: {bad_class} non-residential row(s) "
                        f"carry a customer_class (residential-only field)"))
    return out


def _dim_tuples(df) -> set[tuple[str, ...]]:
    return set(df[DIMENSION_COLUMNS].astype(str).itertuples(index=False, name=None))


# ===========================================================================
# 5. The check functions (DAG nodes)
# ===========================================================================
def _present(feed):
    def fn(frames, _contracts):
        if frames.get(feed) is None:
            return [_err(f"{feed}: required table is missing")]
        return []
    return fn


def _schema(feed):
    required = REQUIRED_COLUMNS[feed]

    def fn(frames, _contracts):
        df = frames[feed]
        cols = list(df.columns)
        missing = [c for c in required if c not in cols]
        extra = [c for c in cols if c not in required]
        out = []
        if missing:
            out.append(_err(f"{feed}.csv: missing required column(s): {missing}"))
        if extra:
            out.append(_warn(f"{feed}.csv: unexpected column(s) (ignored downstream): {extra}"))
        return out
    return fn


def _content_sales(frames, contracts):
    df = frames["sales"]
    out = _check_nonnull(df, "sales", ["customer_key", "sale_date",
                                       "entity", "region", "service_territory",
                                       "segment", "product_type", "customer_size_tier"])
    out += _check_numeric(df, "sales", "customer_key", integer=True, nonneg=True)
    out += _check_dates(df, "sales", ["sale_date"], contracts, allow_future=False)
    for col in ("segment", "product_type", "customer_size_tier", "customer_class"):
        out += _check_enum(df, "sales", col)
    out += _check_dim_conditionals(df, "sales")
    return out


def _content_conversions(frames, contracts):
    df = frames["conversions"]
    out = _check_nonnull(df, "conversions", ["customer_key", "sale_date", "conversion_date",
                                             "entity", "region", "service_territory",
                                             "segment", "product_type", "customer_size_tier"])
    out += _check_numeric(df, "conversions", "customer_key", integer=True, nonneg=True)
    out += _check_dates(df, "conversions", ["sale_date", "conversion_date"],
                        contracts, allow_future=False)
    out += _check_numeric(df, "conversions", "price_per_unit", nonneg=True, nullable=True)
    for col in ("segment", "product_type", "customer_size_tier", "customer_class"):
        out += _check_enum(df, "conversions", col)
    out += _check_dim_conditionals(df, "conversions")
    # within-row temporal rule: a gain cannot land before its submission.
    both = _nonempty_mask(df["sale_date"]) & _nonempty_mask(df["conversion_date"])
    sd = pd.to_datetime(df["sale_date"][both], format="%Y-%m-%d", errors="coerce")
    cd = pd.to_datetime(df["conversion_date"][both], format="%Y-%m-%d", errors="coerce")
    backwards = (cd < sd).sum()
    if backwards:
        out.append(_err(f"{_loc('conversions', 'conversion_date', 'sale_date')}: {backwards} "
                        f"row(s) where conversion_date precedes sale_date"))
    return out


def _content_gl(frames, contracts):
    df = frames["gl_actuals"]
    out = _check_nonnull(df, "gl_actuals", ["posting_date", "document_date",
                                            "cost_center", "cost_center_description",
                                            "gl_account", "gl_account_description",
                                            "amount", "vendor"])
    out += _check_dates(df, "gl_actuals", ["posting_date", "document_date"],
                        contracts, allow_future=False)
    out += _check_numeric(df, "gl_actuals", "amount", nonneg=True)
    return out


def _content_reference(frames, contracts):
    df = frames["reference_data"]
    out = _check_nonnull(df, "reference_data",
                         ["date", "entity", "region", "service_territory", "segment",
                          "product_type", "customer_size_tier", "reference_type",
                          "volume_in_ref", "volume_converted_ref", "cost_ref",
                          "cpa_ref", "cogs_ref", "ltv_ref", "margin_ref"])
    # plan/forecast dates may legitimately fall in future periods → parse only.
    out += _check_dates(df, "reference_data", ["date"], contracts, allow_future=True)
    out += _check_enum(df, "reference_data", "reference_type")
    for col in ("volume_in_ref", "volume_converted_ref"):
        out += _check_numeric(df, "reference_data", col, integer=True, nonneg=True)
    for col in ("cost_ref", "cpa_ref", "cogs_ref", "ltv_ref"):
        out += _check_numeric(df, "reference_data", col, nonneg=True)
    out += _check_numeric(df, "reference_data", "margin_ref")  # margin may be negative
    for col in ("segment", "product_type", "customer_size_tier", "customer_class"):
        out += _check_enum(df, "reference_data", col)
    out += _check_dim_conditionals(df, "reference_data")
    return out


def _content_notes(frames, contracts):
    df = frames["operational_notes"]
    out = _check_nonnull(df, "operational_notes",
                         ["date", "entity", "region", "segment", "note_text"])
    out += _check_dates(df, "operational_notes", ["date"], contracts, allow_future=False)
    return out


def _gl_combos_resolve(frames, contracts):
    df = frames["gl_actuals"]
    combos = set(df[["cost_center", "gl_account", "vendor"]].itertuples(index=False, name=None))
    unresolved = combos - contracts.gl_combos
    if unresolved:
        return [_err(f"{_loc('gl_actuals', 'cost_center', 'gl_account', 'vendor')}: "
                     f"{len(unresolved)} combo(s) not resolvable via gl_mapping.csv: "
                     f"{_sample(unresolved)}")]
    return []


def _dim_tuples_known(frames, contracts):
    out = []
    for feed in ("sales", "conversions", "reference_data"):
        unknown = _dim_tuples(frames[feed]) - contracts.leaf_tuples
        if unknown:
            out.append(_err(f"{_loc(feed, *DIMENSION_COLUMNS)}: {len(unknown)} dimension "
                            f"tuple(s) not in the roster (cogs_config.csv leaves): "
                            f"{_sample(unknown, n=3)}"))
    return out


def _keys_unique(frames, _contracts):
    out = []
    for feed in ("sales", "conversions"):
        dup = frames[feed]["customer_key"].duplicated().sum()
        if dup:
            out.append(_err(f"{_loc(feed, 'customer_key')}: {dup} duplicate value(s)"))
    return out


def _conversions_reference_sales(frames, _contracts):
    orphans = set(frames["conversions"]["customer_key"]) - set(frames["sales"]["customer_key"])
    if orphans:
        return [_err(f"{_loc('conversions', 'customer_key')}: {len(orphans)} value(s) with no "
                     f"matching submission in sales.csv: {_sample(orphans)}")]
    return []


def _facts_have_reference(frames, _contracts):
    """Every (entity, segment) appearing in the operational facts must have a
    reference_data row — otherwise there is no plan/forecast to measure that unit
    against, and a variance can't be computed. ERROR (this is a hard coverage gap)."""
    ref_units = set(frames["reference_data"][["entity", "segment"]]
                    .itertuples(index=False, name=None))
    out = []
    for feed in ("sales", "conversions"):
        df = frames[feed]
        missing = set(df[["entity", "segment"]].itertuples(index=False, name=None)) - ref_units
        if missing:
            out.append(_err(f"{_loc(feed, 'entity', 'segment')}: {len(missing)} entity/segment "
                            f"combo(s) with no matching row in reference_data.csv: "
                            f"{_sample(missing)}"))
    return out


def _notes_scope_known(frames, contracts):
    df = frames["operational_notes"]
    bad = []
    for entity, region, segment in df[["entity", "region", "segment"]].itertuples(index=False, name=None):
        if NOTE_WILDCARD in (entity, region, segment):
            continue  # 'ALL' is a per-level wildcard
        if (entity, region, segment) not in contracts.valid_note_scopes:
            bad.append((entity, region, segment))
    if bad:
        return [_err(f"{_loc('operational_notes', 'entity', 'region', 'segment')}: "
                     f"{len(bad)} note(s) scoped to an unknown (entity, region, segment): "
                     f"{_sample(set(bad))}")]
    return []


def _plan_covers_acq_units(frames, contracts):
    """GL tie-back completeness: every acquisition unit the ledger maps to should have
    an active-period plan row, so every actual GL dollar has a plan CPA to compare
    against. WARNING — surfaced, not fatal (the metrics layer falls back to estimates)."""
    ref = frames["reference_data"]
    plan = ref[(ref["reference_type"] == "plan")
               & ref["date"].astype(str).str.startswith(contracts.active_period)]
    plan_units = set(plan[["entity", "region", "segment"]].itertuples(index=False, name=None))
    uncovered = contracts.acq_units - plan_units
    if uncovered:
        return [_warn(f"reference_data: {len(uncovered)} acquisition unit(s) from gl_mapping "
                      f"with no {contracts.active_period} plan row: {_sample(uncovered)}")]
    return []


# ===========================================================================
# 6. DAG assembly + execution
# ===========================================================================
def build_dag() -> list[Check]:
    """The validation DAG, in declaration order. Dependencies are the edges; the
    runner topologically sorts and skips any check whose upstream failed."""
    checks: list[Check] = []

    # T0 presence — one node per feed, no dependencies.
    for feed in FEEDS:
        checks.append(Check(f"present:{feed}", (), _present(feed)))
    # T1 schema — required columns present (depends on the feed existing).
    for feed in FEEDS:
        checks.append(Check(f"schema:{feed}", (f"present:{feed}",), _schema(feed)))
    # T2 content — type / nullability / enum / cross-field, per feed.
    content_fns = {
        "sales": _content_sales, "conversions": _content_conversions,
        "gl_actuals": _content_gl, "reference_data": _content_reference,
        "operational_notes": _content_notes,
    }
    for feed, fn in content_fns.items():
        checks.append(Check(f"content:{feed}", (f"schema:{feed}",), fn))
    # T3 cross-feed / join integrity — depend on the relevant content nodes.
    checks += [
        Check("gl_combos_resolve", ("content:gl_actuals",), _gl_combos_resolve),
        Check("dim_tuples_known",
              ("content:sales", "content:conversions", "content:reference_data"),
              _dim_tuples_known),
        Check("keys_unique", ("content:sales", "content:conversions"), _keys_unique),
        Check("conversions_reference_sales",
              ("content:sales", "content:conversions"), _conversions_reference_sales),
        Check("facts_have_reference",
              ("content:sales", "content:conversions", "content:reference_data"),
              _facts_have_reference),
        Check("notes_scope_known", ("content:operational_notes",), _notes_scope_known),
        Check("plan_covers_acq_units", ("content:reference_data",), _plan_covers_acq_units),
    ]
    return checks


def _toposort(checks: list[Check]) -> list[Check]:
    """Kahn's algorithm, stable on declaration order (deterministic run sequence).
    Raises on an unknown dependency or a cycle — that's a programming error in the
    DAG, not a data problem, so it should blow up immediately."""
    by_name = {c.name: c for c in checks}
    indegree = {c.name: 0 for c in checks}
    dependents: dict[str, list[str]] = {c.name: [] for c in checks}
    for c in checks:
        for dep in c.depends_on:
            if dep not in by_name:
                raise ValueError(f"check {c.name!r} depends on unknown check {dep!r}")
            indegree[c.name] += 1
            dependents[dep].append(c.name)

    ready = deque(c.name for c in checks if indegree[c.name] == 0)
    order: list[Check] = []
    while ready:
        name = ready.popleft()
        order.append(by_name[name])
        for child in dependents[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(order) != len(checks):
        raise ValueError("validation DAG has a cycle")
    return order


def validate_ingestion(frames: Frames, contracts: Contracts,
                       snapshot: str = "(in-memory)") -> ValidationReport:
    """Run the DAG over already-loaded frames and return the aggregated report.

    Pure: it does not raise on bad data — call ``report.raise_if_failed()`` to enforce
    the loud halt. This split keeps the gate testable and lets callers decide policy.
    """
    report = ValidationReport(snapshot=snapshot)
    done: dict[str, CheckResult] = {}

    for check in _toposort(build_dag()):
        # Skip if any dependency failed or was itself skipped.
        blockers = [d for d in check.depends_on if done[d].blocks_dependents]
        if blockers:
            res = CheckResult(check.name, Status.SKIPPED,
                              skip_reason=f"blocked by {', '.join(blockers)}")
        else:
            problems = check.fn(frames, contracts)
            if any(p.severity == Severity.ERROR for p in problems):
                status = Status.FAILED
            elif problems:
                status = Status.WARNED
            else:
                status = Status.PASSED
            res = CheckResult(check.name, status, problems)
        done[check.name] = res
        report.results.append(res)

    return report


# ===========================================================================
# 7. Loader + directory entry point
# ===========================================================================
def read_snapshot_frames(snapshot_dir: Path) -> Frames:
    """Thin stopgap loader: read the five per-table CSVs from a snapshot folder.

    A missing table yields ``None`` (the presence check turns that into a halt).
    Real production loading (live sources, XLSX, dtype policy) lands in step-3's
    ``data_loader.py``; this is only enough to feed the gate from disk."""
    snapshot_dir = Path(snapshot_dir)
    frames: Frames = {}
    for feed in FEEDS:
        path = snapshot_dir / f"{feed}.csv"
        frames[feed] = _read_csv(path) if path.exists() else None
    return frames


def _infer_as_of(snapshot_dir: Path, default: dt.date) -> dt.date:
    """A snapshot folder is self-dating: a cumulative cut named ``YYYY-MM-DD`` has that
    date as its freshness cutoff. Fall back to the config's active snapshot date for a
    folder that isn't date-named (e.g. a fixture)."""
    try:
        return dt.date.fromisoformat(snapshot_dir.name)
    except ValueError:
        return default


def validate_snapshot_dir(snapshot_dir: Path,
                          contracts: Optional[Contracts] = None,
                          as_of: Optional[dt.date] = None) -> ValidationReport:
    """Load a snapshot folder + contracts, run the gate, return the report.

    The "not in the future" cutoff is the snapshot's own as-of date (inferred from the
    folder name unless given) — *not* the config's active-snapshot pointer — so each
    cumulative cut validates against the day it represents. The business
    ``active_period`` used for plan coverage stays as configured."""
    contracts = contracts or load_contracts()
    snapshot_dir = Path(snapshot_dir)
    as_of = as_of or _infer_as_of(snapshot_dir, contracts.snapshot_date)
    run_contracts = dataclasses.replace(contracts, snapshot_date=as_of)
    frames = read_snapshot_frames(snapshot_dir)
    return validate_ingestion(frames, run_contracts, snapshot=str(snapshot_dir))


# ===========================================================================
# 8. CLI
# ===========================================================================
def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m app.validation.ingestion_validator <snapshot_dir>",
              file=sys.stderr)
        return 2
    report = validate_snapshot_dir(Path(argv[0]))
    print(report.render())
    if not report.ok:
        # Loud halt: non-zero exit, report already on stdout (§17).
        print("\nPIPELINE HALTED — ingestion validation failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
