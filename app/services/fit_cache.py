"""Stage 21: persistent vacancy assessment cache (docs/STAGE21_VACANCY_CACHE_PLAN.md).

`job_fit_assessments` holds one `assess_job` result per vacancy together with
an `input_key`: a SHA-256 over every input the assessment reads. The key is
recomputed from the current database (SQL plus hashing, never `assess_job`);
a cached row counts only while its stored key equals the current one, so a
stale tier is never presented as current:

- rules: the source of `app.services.job_fit_assessment`, this module and
  every `app.*` module in their transitive import closure;
- the candidate profile version (profile and search configuration changes
  increment it);
- the vacancy row: every `jobs` column except `JOB_COLUMNS_EXCLUDED`;
- the latest matcher analysis (id, its job snapshot fingerprint and profile
  version) and the latest commute check (same ordering as
  `latest_commute_check`);
- an applications watermark: a hash over every column of every
  application row plus the highest event id. Any application change, also
  an edit of an older row, marks every row stale (Codex Stage 21 review:
  count / max id / max timestamp missed such edits);
- the descriptions of the jobs the applications point to (Stage 22): the
  duplicate criterion compares each vacancy's text with them
  (SIMILAR_DESCRIPTION), so an edit of such a job row -- or its deletion --
  marks every OTHER vacancy's row stale too, not only that job's own row.

Writes happen only through `upsert_assessment`, called by the offline
`scripts/stage21_assess_vacancies.py`; the UI only reads.

A stored key is only as good as the state `assess_job` read: if the key and
the assessment come from different states (A, then B), a later return to A
would make B's assessment read FRESH (Codex audit of 76e8dc1). A writer
therefore reads the key, the observed cache row and the assessment inside
ONE transaction that sees one state (`require_consistent_reads`):
PostgreSQL REPEATABLE READ (one snapshot; a concurrent change to a row it
writes is a serialization failure, never a lost update) or a SQLite
transaction begun before the first read (the writer's `BEGIN IMMEDIATE`
also keeps every other writer out until it commits).
"""

import ast
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from sqlalchemy import false, func, inspect, select
from sqlalchemy.orm import Session

from app.db.models import (
    ApplicationEventRecord,
    ApplicationRecord,
    CandidateJobMatchRecord,
    CandidateProfileRecord,
    JobCommuteCheckRecord,
    JobFitAssessmentRecord,
    JobRecord,
)
from app.services.job_fit_assessment import FitAssessment

CacheState = Literal["FRESH", "STALE", "NOT_ASSESSED"]
CacheStatus = Literal["MISSING", "EMPTY", "IN_USE"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULE_MODULES = ("app.services.job_fit_assessment", "app.services.fit_cache")
# `last_seen_at` changes on every collection run and no criterion reads it
# (a test checks the rules' import closure never mentions an excluded column).
JOB_COLUMNS_EXCLUDED = frozenset({"last_seen_at"})
_FULLY_REMOTE = frozenset({"COMPATIBLE", "INCOMPATIBLE", "NOT_CONFIGURED"})
_HYBRID = frozenset({"HYBRID", "PARTIAL_REMOTE"})


# --- derived list fields (also used by the UI) ---------------------------------


def work_mode(assessment: FitAssessment) -> str:
    status = assessment.criterion("remote").status
    if status in _FULLY_REMOTE:
        return "REMOTE"
    if status in _HYBRID:
        return "HYBRID"
    return status  # ONSITE / UNKNOWN


def applied(assessment: FitAssessment) -> bool:
    return assessment.criterion("duplicate").status == "ALREADY_APPLIED"


# --- rules hash -----------------------------------------------------------------


def _module_file(root: Path, name: str) -> Path | None:
    base = root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_names(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def rule_closure(root: Path = PROJECT_ROOT) -> dict[str, Path]:
    """Every `app.*` module (with its parent packages) transitively imported
    from RULE_MODULES, including function-level imports."""
    files: dict[str, Path] = {}
    seen: set[str] = set()
    stack = list(RULE_MODULES)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        stack.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        path = _module_file(root, name)
        if path is None:
            continue  # an imported attribute (class/function), not a module
        files[name] = path
        stack.extend(n for n in _imported_names(path) if n.split(".")[0] == "app")
    return files


def compute_rules_hash(root: Path = PROJECT_ROOT) -> str:
    digest = hashlib.sha256()
    for name, path in sorted(rule_closure(root).items()):
        source = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + hashlib.sha256(source).digest())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def rules_hash() -> str:
    """Cached per process: the code cannot change under a running process."""
    return compute_rules_hash()


# --- input key ------------------------------------------------------------------


def _plain(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        # independent of the PostgreSQL session's TimeZone setting
        return value.astimezone(UTC).isoformat()
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def hashed_job_columns() -> list[str]:
    return sorted(c.name for c in JobRecord.__table__.columns if c.name not in JOB_COLUMNS_EXCLUDED)


def job_row_hash(values: dict) -> str:
    payload = [[name, _plain(values[name])] for name in hashed_job_columns()]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class GlobalInputs:
    rules_hash: str
    profile_version: int | None
    applications_watermark: str


def global_inputs(session: Session) -> GlobalInputs:
    profile_version = session.scalar(
        select(CandidateProfileRecord.profile_version).order_by(CandidateProfileRecord.id).limit(1)
    )
    max_event = session.scalar(select(func.max(ApplicationEventRecord.id)))
    watermark = f"{applications_hash(session)}:{applied_jobs_hash(session)}:{max_event}"
    return GlobalInputs(rules_hash(), profile_version, watermark)


def applications_hash(session: Session) -> str:
    """Every column of every application (a few dozen rows; the duplicate
    criterion reads all of them)."""
    table = ApplicationRecord.__table__
    names = sorted(c.name for c in table.columns)
    digest = hashlib.sha256()
    for row in session.execute(select(table).order_by(table.c.id)).mappings():
        payload = [[name, _plain(row[name])] for name in names]
        digest.update(json.dumps(payload, ensure_ascii=False, default=str).encode() + b"\0")
    return digest.hexdigest()


def applied_jobs_hash(session: Session) -> str:
    """The description of every job row an application points to (sorted by
    job id; a referenced but missing row hashes as absent). `applications.job_id`
    has no foreign key, so the row may vanish independently."""
    job_ids = sorted(
        {
            job_id
            for job_id in session.scalars(select(ApplicationRecord.job_id))
            if job_id is not None
        }
    )
    present = (
        dict(
            session.execute(
                select(JobRecord.id, JobRecord.description).where(JobRecord.id.in_(job_ids))
            ).all()
        )
        if job_ids
        else {}
    )
    payload = [[job_id, job_id in present, present.get(job_id)] for job_id in job_ids]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _in(column, job_ids: Sequence[int] | None):
    return [] if job_ids is None else [column.in_(job_ids)]


def latest_match_keys(session: Session, job_ids: Sequence[int] | None) -> dict[int, str]:
    rows = session.execute(
        select(
            CandidateJobMatchRecord.job_id,
            CandidateJobMatchRecord.id,
            CandidateJobMatchRecord.job_snapshot_fingerprint,
            CandidateJobMatchRecord.candidate_profile_version,
        )
        .where(*_in(CandidateJobMatchRecord.job_id, job_ids))
        .order_by(CandidateJobMatchRecord.job_id, CandidateJobMatchRecord.id)
    ).all()
    return {job_id: f"{match_id}:{fp}:{version}" for job_id, match_id, fp, version in rows}


def latest_commute_keys(session: Session, job_ids: Sequence[int] | None) -> dict[int, str]:
    rows = session.execute(
        select(JobCommuteCheckRecord.job_id, JobCommuteCheckRecord.id)
        .where(*_in(JobCommuteCheckRecord.job_id, job_ids))
        .order_by(
            JobCommuteCheckRecord.job_id,
            JobCommuteCheckRecord.checked_at,
            JobCommuteCheckRecord.id,
        )
    ).all()
    return {job_id: str(check_id) for job_id, check_id in rows}


def input_key(inputs: GlobalInputs, job_hash: str, match_key: str, commute_key: str) -> str:
    parts = [
        inputs.rules_hash,
        str(inputs.profile_version),
        job_hash,
        match_key,
        commute_key,
        inputs.applications_watermark,
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _chunks(ids: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def current_keys(
    session: Session, job_ids: Sequence[int] | None = None, inputs: GlobalInputs | None = None
) -> dict[int, str]:
    """The current input key of each given vacancy (all when None). SQL and
    hashing only -- never `assess_job`."""
    inputs = inputs or global_inputs(session)
    columns = [JobRecord.__table__.c[name] for name in hashed_job_columns()]
    batches = [None] if job_ids is None else list(_chunks(list(job_ids)))
    keys: dict[int, str] = {}
    for batch in batches:
        matches = latest_match_keys(session, batch)
        commutes = latest_commute_keys(session, batch)
        for row in session.execute(select(*columns).where(*_in(JobRecord.id, batch))).mappings():
            job_id = row["id"]
            keys[job_id] = input_key(
                inputs, job_row_hash(row), matches.get(job_id, "-"), commutes.get(job_id, "-")
            )
    return keys


# --- cache table ----------------------------------------------------------------


def cache_table_present(session: Session) -> bool:
    return inspect(session.get_bind()).has_table(JobFitAssessmentRecord.__tablename__)


def cache_status(session: Session) -> CacheStatus:
    """MISSING: no Stage 21 table; EMPTY: never filled; IN_USE: filled at
    least once. Until IN_USE the UI keeps its Stage 20 behaviour and says so."""
    if not cache_table_present(session):
        return "MISSING"
    filled = session.scalar(select(JobFitAssessmentRecord.job_id).limit(1)) is not None
    return "IN_USE" if filled else "EMPTY"


def cache_in_use(session: Session) -> bool:
    return cache_status(session) == "IN_USE"


def cached_keys(session: Session, job_ids: Sequence[int] | None = None) -> dict[int, str]:
    keys: dict[int, str] = {}
    batches = [None] if job_ids is None else list(_chunks(list(job_ids)))
    for batch in batches:
        rows = session.execute(
            select(JobFitAssessmentRecord.job_id, JobFitAssessmentRecord.input_key).where(
                *_in(JobFitAssessmentRecord.job_id, batch)
            )
        )
        keys.update(dict(rows.all()))
    return keys


def cache_state(stored_key: str | None, current_key: str | None) -> CacheState:
    if stored_key is None:
        return "NOT_ASSESSED"
    return "FRESH" if stored_key == current_key else "STALE"


def assessment_values(assessment: FitAssessment) -> dict:
    return {
        "fit_tier": assessment.fit_tier,
        "decision": assessment.decision,
        "criteria_json": json.dumps(
            [asdict(c) for c in assessment.criteria], ensure_ascii=False, sort_keys=True
        ),
        "tier_reasons_json": json.dumps(assessment.tier_reasons, ensure_ascii=False),
        "work_mode": work_mode(assessment),
        "applied": applied(assessment),
    }


class InconsistentReadError(RuntimeError):
    """The session's reads would not all see one database state."""


_SNAPSHOT_LEVELS = frozenset({"repeatable read", "serializable"})


def require_consistent_reads(session: Session) -> None:
    """Refuse unless every read the session makes from now on sees the same
    state until it commits (see the module docstring). Call it before the
    first read of a write transaction."""
    connection = session.connection()
    dialect = connection.dialect.name
    if dialect == "postgresql":
        level = connection.exec_driver_sql("SHOW transaction_isolation").scalar()
        consistent = level in _SNAPSHOT_LEVELS
    elif dialect == "sqlite":
        # pysqlite's default sends BEGIN only before the first write, so each
        # SELECT before it sees its own state; an explicit BEGIN fixes one.
        consistent = connection.connection.dbapi_connection.in_transaction
    else:  # pragma: no cover - the project supports SQLite and PostgreSQL only
        consistent = False
    if not consistent:
        raise InconsistentReadError(
            f"{dialect}: the cache writer needs one consistent read transaction "
            "(PostgreSQL REPEATABLE READ, SQLite BEGIN before the first read)"
        )


_UNCONDITIONAL = object()


def upsert_assessment(
    session: Session,
    *,
    job_id: int,
    key: str,
    inputs: GlobalInputs,
    assessment: FitAssessment,
    now: datetime | None = None,
    expected_key: str | None | object = _UNCONDITIONAL,
) -> bool:
    """Insert or replace the vacancy's row (one statement, so two concurrent
    writers leave one consistent row). The caller owns the transaction.

    With `expected_key` (the stored key the caller read before assessing,
    None for no row) the write is a compare-and-swap: it lands only while the
    stored row is still the one observed, so a writer that assessed older
    inputs never replaces a row another writer stored meanwhile (Codex
    Stage 21 audit). Returns whether a row was written."""
    values = {
        "job_id": job_id,
        "input_key": key,
        "rules_hash": inputs.rules_hash,
        "profile_version": inputs.profile_version,
        "computed_at": now or datetime.now(UTC),
        **assessment_values(assessment),
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - the project supports SQLite and PostgreSQL only
        raise RuntimeError(f"unsupported database dialect {dialect}")
    statement = insert(JobFitAssessmentRecord).values(**values)
    updated = {k: statement.excluded[k] for k in values if k != "job_id"}
    if expected_key is _UNCONDITIONAL:
        where = None
    elif expected_key is None:
        where = false()  # a row appeared since the caller looked: keep it
    else:
        where = JobFitAssessmentRecord.__table__.c.input_key == expected_key
    # RETURNING, not rowcount: psycopg reports -1 for this statement
    statement = statement.on_conflict_do_update(
        index_elements=["job_id"], set_=updated, where=where
    ).returning(JobFitAssessmentRecord.job_id)
    return session.execute(statement).first() is not None
