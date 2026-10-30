"""Codex audit of 76e8dc1, P2-2: the production runtime database is
recognised from every checkout, not only from the one the code runs in.

A real Git repository with a linked worktree is built under tmp_path; the
"production" file of its main checkout is a disposable stand-in. The real
production path of this repository is only classified (realpath/stat),
never opened.
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

import scripts.stage15_application_tracker as tracker
from app.core import database_target as dt
from app.core.database_target import DatabaseKind, classify_path
from app.db.models import Base, JobFitAssessmentRecord, JobRecord
from app.services.readiness import expected_alembic_heads
from scripts import stage21_assess_vacancies as writer_cli


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def worktrees(tmp_path):
    """(main checkout, linked worktree) of a throwaway repository."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    main = tmp_path / "job-triage"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init")
    _git(main, "worktree", "add", "-q", "-b", "wt", str(tmp_path / "job-triage-wt"))
    return main, tmp_path / "job-triage-wt"


def _stamped_database(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
        connection.execute(
            JobRecord.__table__.insert().values(
                fingerprint="fp-1",
                source="test",
                title="Python Entwickler (m/w/d)",
                company="Beispiel GmbH",
                url="https://jobs.example.invalid/1",
                description="Python, SQL. Deutsch B1.",
                score=60,
                recommendation="MAYBE",
            )
        )
    engine.dispose()
    return path


def _run_from(monkeypatch, worktree: Path) -> None:
    """Make the module behave as if this code ran from `worktree`."""
    monkeypatch.setattr(dt, "PRODUCTION_RUNTIME_DB", worktree / "runtime" / dt.PRODUCTION_DB_NAME)
    roots = dt.checkout_roots(worktree)
    monkeypatch.setattr(
        dt,
        "known_production_databases",
        lambda: tuple(root / "runtime" / dt.PRODUCTION_DB_NAME for root in roots),
    )


@pytest.fixture()
def opened(monkeypatch):
    """Every path sqlite3 or the tracker's hash opens."""
    seen: list[str] = []
    real_connect = sqlite3.connect
    real_sha = tracker._sha256

    def connect(database, *args, **kwargs):
        seen.append(str(database))
        return real_connect(database, *args, **kwargs)

    def sha(path):
        seen.append(str(path))
        return real_sha(path)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(tracker, "_sha256", sha)
    return seen


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hard_link(source: Path, link: Path) -> Path:
    try:
        os.link(source, link)
    except OSError as exc:  # pragma: no cover - filesystem without hard links
        pytest.skip(f"hard links unsupported: {exc}")
    return link


def _symlink(source: Path, link: Path, *, directory: bool = False) -> Path:
    try:
        os.symlink(source, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unsupported here: {exc}")
    return link


# --- discovery ----------------------------------------------------------------


def test_every_worktree_of_the_repository_is_found(worktrees):
    main, linked = worktrees
    expected = {Path(dt._identity(main)), Path(dt._identity(linked))}
    assert set(dt.checkout_roots(linked)) == expected
    assert set(dt.checkout_roots(main)) == expected


def test_a_separate_git_dir_main_checkout_is_found_through_core_worktree(tmp_path, monkeypatch):
    """Codex round 5: the common directory is `repo.git`, not `.git`; the
    main worktree is recorded in core.worktree."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    main = tmp_path / "main"
    _git(tmp_path, "init", "-q", "--separate-git-dir", str(tmp_path / "repo.git"), str(main))
    _git(main, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init")
    _git(main, "config", "core.worktree", str(main))
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "wt", str(linked))
    assert Path(dt._identity(main)) in dt.checkout_roots(linked)
    _run_from(monkeypatch, linked)
    production = main / "runtime" / dt.PRODUCTION_DB_NAME
    production.parent.mkdir()
    production.write_bytes(b"stand-in")
    alias = _hard_link(production, tmp_path / "renamed.db")
    assert classify_path(alias) is DatabaseKind.PRODUCTION_RUNTIME
    monkeypatch.setenv("DATABASE_ROLE", "test")
    with pytest.raises(tracker.TargetRefusedError, match="production runtime"):
        tracker._refuse_kind_for_cli(alias, writes=False)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _common_dir(tmp_path) -> Path:
    """A real Git directory (as `--separate-git-dir` leaves it) to configure."""
    common = tmp_path / "repo.git"
    _git(tmp_path, "init", "-q", "--bare", str(common))
    return common


def _append(path: Path, text_: str, *, prefix: bytes = b"") -> None:
    with path.open("ab") as handle:
        handle.write(prefix + text_.encode("utf-8"))


def _candidates(common: Path) -> set[Path]:
    return {Path(dt._identity(p)) for p in dt._core_worktrees(common)}


def _expected(common: Path, relative: str) -> Path:
    return Path(dt._identity(common / relative))


BACKSLASH = chr(92)


@needs_git
@pytest.mark.parametrize(
    ("config", "relative"),
    [
        # Codex round 6: comments and last value
        ("[core]\n\tworktree = ../first\n[core] ; again\n\tworktree = ../main # c\n", "../main"),
        # Codex round 9/10: continuation; a comment ending in a backslash
        ("[core]\n\tworktree = ../ma" + BACKSLASH + "\nin\n", "../main"),
        ("[core]\n# note " + BACKSLASH + "\n\tworktree = ../main\n", "../main"),
        # Codex round 10: empty quotes, quoted blanks
        ('[core]\n\tworktree = "" ../main\n', "../main"),
        ('[core]\n\tworktree = "../ma in"\n', "../ma in"),
    ],
)
def test_core_worktree_is_read_by_git_itself(tmp_path, config, relative):
    common = _common_dir(tmp_path)
    _append(common / "config", config)
    assert _expected(common, relative) in _candidates(common)


@needs_git
def test_core_worktree_from_includes_conditions_and_non_ascii_paths(tmp_path, monkeypatch):
    """Codex rounds 7-8 and 10: includes, `includeIf "gitdir:..."` evaluated
    for this repository even when the process runs in another repository,
    a non-ASCII path decoded as UTF-8, a BOM-prefixed include file."""
    common = _common_dir(tmp_path)
    name = "../M" + chr(0xFC) + "ller"
    _append(common / "nested.config", "[core]\n\tworktree = " + name + "\n", prefix=b"\xef\xbb\xbf")
    _append(common / "extra.config", "[include]\n\tpath = nested.config\n")
    gitdir = common.resolve().as_posix()
    _append(common / "config", f'[includeIf "gitdir:{gitdir}/"]\n\tpath = extra.config\n')
    monkeypatch.chdir(dt.PROJECT_ROOT)  # a different repository's context
    monkeypatch.setenv("GIT_DIR", str(dt.PROJECT_ROOT / ".git"))
    assert _expected(common, name) in _candidates(common)


@needs_git
def test_core_worktree_from_the_main_worktree_config(tmp_path):
    """Codex round 10: with extensions.worktreeConfig the main worktree's
    `config.worktree` can hold core.worktree."""
    common = _common_dir(tmp_path)
    _git(common, "config", "core.repositoryformatversion", "1")
    _git(common, "config", "extensions.worktreeConfig", "true")
    _append(common / "config.worktree", "[core]\n\tworktree = ../main\n")
    assert _expected(common, "../main") in _candidates(common)


def test_without_git_no_core_worktree_candidate_is_invented(tmp_path, monkeypatch):
    """Git unavailable: nothing is guessed (residual risk, docs section 11)."""

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(dt.subprocess, "run", no_git)
    assert dt._core_worktrees(tmp_path) == []


def test_git_output_keeps_every_byte_of_a_path(monkeypatch):
    """Codex round 11: CR and non-UTF-8 bytes are path characters on POSIX;
    no newline translation, no lossy decoding."""
    raw = b"../ma\rin\0../caf\xe9\0"

    def fake_git(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=raw, stderr=b"")

    monkeypatch.setattr(dt.subprocess, "run", fake_git)
    values = dt._git_core_worktrees(Path("."))
    assert values == ["../ma\rin", "../caf" + chr(0xDCE9)]
    if os.name != "nt":  # POSIX: the original bytes come back
        assert os.fsencode(values[1]) == b"../caf\xe9"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"gitdir: /repo/.git/worktrees/wt" + chr(0xA0).encode() + b"\n", "wt" + chr(0xA0)),
        (b"gitdir:  /repo/.git/worktrees/ wt\r\n", " wt"),
        (b"gitdir: /repo/.git/worktrees/caf\xe9\n", "caf" + chr(0xDCE9)),
    ],
)
def test_git_metadata_paths_keep_their_whitespace_and_bytes(tmp_path, content, expected):
    """Codex round 11: like Git, only the exact `gitdir: ` prefix and the
    trailing CR/LF are removed."""
    pointer = tmp_path / ".git"
    pointer.write_bytes(content)
    assert dt._read_pointer(pointer, "gitdir: ").name == expected


def test_unreadable_or_missing_config_adds_no_candidate(tmp_path):
    assert dt._core_worktrees(tmp_path / "missing") == []


def test_a_directory_without_git_metadata_is_its_own_only_root(tmp_path):
    assert dt.checkout_roots(tmp_path) == [Path(dt._identity(tmp_path))]


def test_this_checkout_knows_its_own_and_its_main_checkouts_database():
    known = {dt._identity(p) for p in dt.known_production_databases()}
    assert dt._identity(dt.PRODUCTION_RUNTIME_DB) in known
    dotgit = dt.PROJECT_ROOT / ".git"
    if dotgit.is_file():  # running from a linked worktree (local Stage 21 work)
        common = dt._read_pointer(dt._read_pointer(dotgit, "gitdir: ") / "commondir")
        main = Path(os.path.realpath(common)).parent
        assert dt._identity(main / "runtime" / dt.PRODUCTION_DB_NAME) in known


def test_the_real_main_checkout_database_is_refused_from_a_worktree_without_opening_it(
    monkeypatch, opened
):
    """The actual cross-worktree case of the audit: only classified."""
    monkeypatch.setenv("DATABASE_ROLE", "test")
    others = [
        p
        for p in dt.known_production_databases()
        if dt._identity(p) != dt._identity(dt.PRODUCTION_RUNTIME_DB)
    ]
    if not others:
        pytest.skip("this checkout has no other worktree")
    for production in others:
        assert classify_path(production) is DatabaseKind.PRODUCTION_RUNTIME
        with pytest.raises(tracker.TargetRefusedError, match="production runtime"):
            tracker.resolve_engine(str(production), None, writes=True)
        assert writer_cli.run(["--database", str(production), "--apply"]) == 2
    assert opened == []


# --- classification from another worktree --------------------------------------


def test_main_checkout_database_is_production_from_the_linked_worktree(
    worktrees, monkeypatch, tmp_path
):
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    production = main / "runtime" / dt.PRODUCTION_DB_NAME
    production.parent.mkdir()
    production.write_bytes(b"stand-in")
    assert classify_path(production) is DatabaseKind.PRODUCTION_RUNTIME
    # identity, not only the name: a hard link under another name
    alias = _hard_link(production, tmp_path / "scratch.db")
    assert classify_path(alias) is DatabaseKind.PRODUCTION_RUNTIME
    # relative to the worktree, as an operator would type it
    monkeypatch.chdir(linked)
    relative = Path("..") / "job-triage" / "runtime" / dt.PRODUCTION_DB_NAME
    assert classify_path(relative) is DatabaseKind.PRODUCTION_RUNTIME
    assert dt.classify_database_url(f"sqlite:///{relative.as_posix()}")[0] is (
        DatabaseKind.PRODUCTION_RUNTIME
    )


def test_symlinks_to_the_production_database_are_production(worktrees, monkeypatch, tmp_path):
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    production = main / "runtime" / dt.PRODUCTION_DB_NAME
    production.parent.mkdir()
    production.write_bytes(b"stand-in")
    assert classify_path(_symlink(production, tmp_path / "alias.db")) is (
        DatabaseKind.PRODUCTION_RUNTIME
    )
    folder = _symlink(main / "runtime", tmp_path / "rt", directory=True)
    assert classify_path(folder / dt.PRODUCTION_DB_NAME) is DatabaseKind.PRODUCTION_RUNTIME


@pytest.mark.parametrize(
    "parts",
    [
        ("another-clone", "runtime", "job_triage_runtime.db"),
        ("old-location", "JOB_TRIAGE_RUNTIME.DB"),
    ],
)
def test_the_production_file_name_is_production_anywhere(tmp_path, parts):
    assert classify_path(tmp_path.joinpath(*parts)) is DatabaseKind.PRODUCTION_RUNTIME


@pytest.mark.parametrize(
    ("parts", "kind"),
    [
        (("job-triage-backups", "job_triage_runtime.db"), DatabaseKind.BACKUP),
        (("snap", "job_triage_runtime.pre-stage21-20261030T000000Z.db"), DatabaseKind.BACKUP),
        (("stage21_cache.db",), DatabaseKind.OTHER_SQLITE),
        (("runtime", "job_triage_runtime_copy.db"), DatabaseKind.OTHER_SQLITE),
    ],
)
def test_backups_and_isolated_test_databases_keep_their_kind(tmp_path, parts, kind):
    assert classify_path(tmp_path.joinpath(*parts)) is kind


# --- the CLI guards from another worktree ---------------------------------------


def test_test_role_never_opens_another_worktrees_production_database(
    worktrees, monkeypatch, opened
):
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    production = _stamped_database(main / "runtime" / dt.PRODUCTION_DB_NAME)
    before = _sha(production)
    opened.clear()
    monkeypatch.setenv("DATABASE_ROLE", "test")
    for argv in (["--apply"], ["--apply", "--snapshot", str(linked / "snap")], []):
        assert writer_cli.run(["--database", str(production), *argv]) == 2
    with pytest.raises(tracker.TargetRefusedError, match="production runtime"):
        tracker._refuse_kind_for_cli(production, writes=False)
    assert opened == [] and _sha(production) == before
    assert not (linked / "snap").exists()


def test_production_write_from_another_worktree_needs_a_verified_snapshot(
    worktrees, monkeypatch, opened, capsys
):
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    production = _stamped_database(main / "runtime" / dt.PRODUCTION_DB_NAME)
    before = _sha(production)
    opened.clear()
    monkeypatch.delenv("DATABASE_ROLE", raising=False)
    assert writer_cli.run(["--database", str(production), "--apply"]) == 2
    assert "needs --snapshot" in capsys.readouterr().err
    assert opened == [] and _sha(production) == before  # refused before opening

    snapshots = linked / "snapshots"
    assert (
        writer_cli.run(["--database", str(production), "--apply", "--snapshot", str(snapshots)])
        == 0
    )
    assert "(verified)" in capsys.readouterr().out
    (copy,) = snapshots.iterdir()
    assert classify_path(copy) is DatabaseKind.BACKUP
    with Session(create_engine(f"sqlite:///{production.as_posix()}")) as db:
        assert db.scalar(select(JobFitAssessmentRecord.job_id)) is not None
    with Session(create_engine(f"sqlite:///{copy.as_posix()}")) as db:
        assert db.scalar(select(JobFitAssessmentRecord.job_id)) is None  # taken before writing


def test_a_snapshot_that_fails_verification_stops_before_any_write(worktrees, monkeypatch, capsys):
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    production = _stamped_database(main / "runtime" / dt.PRODUCTION_DB_NAME)
    before = _sha(production)
    monkeypatch.delenv("DATABASE_ROLE", raising=False)

    def broken_copy(_database, target):
        target.write_bytes(b"not a database")

    monkeypatch.setattr(writer_cli, "_backup", broken_copy)
    code = writer_cli.run(
        ["--database", str(production), "--apply", "--snapshot", str(linked / "snapshots")]
    )
    assert code == 2
    assert "nothing written" in capsys.readouterr().err
    assert _sha(production) == before


def test_isolated_test_databases_stay_writable_under_the_test_role(tmp_path, monkeypatch):
    database = _stamped_database(tmp_path / "isolated" / "stage21_cache.db")
    monkeypatch.setenv("DATABASE_ROLE", "test")
    assert classify_path(database) is DatabaseKind.OTHER_SQLITE
    assert writer_cli.run(["--database", str(database), "--apply"]) == 0


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_a_directory_junction_to_the_runtime_folder_is_production(worktrees, monkeypatch, tmp_path):
    """Windows' unprivileged link type (symlinks need a privilege there)."""
    main, linked = worktrees
    _run_from(monkeypatch, linked)
    (main / "runtime").mkdir()
    (main / "runtime" / dt.PRODUCTION_DB_NAME).write_bytes(b"stand-in")
    junction = tmp_path / "rt-junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(main / "runtime")],
        check=True,
        capture_output=True,
    )
    alias = junction / dt.PRODUCTION_DB_NAME
    assert classify_path(alias) is DatabaseKind.PRODUCTION_RUNTIME
    renamed = _hard_link(main / "runtime" / dt.PRODUCTION_DB_NAME, main / "runtime" / "x.db")
    assert classify_path(junction / renamed.name) is DatabaseKind.PRODUCTION_RUNTIME
