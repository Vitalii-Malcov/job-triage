"""Stage 13B: provenance-preserving CandidateProfile transfer
(app/services/candidate_profile_transfer.py, scripts/candidate_profile_transfer_stage13b.py).

SQLite-level: every database here is migrated through the REAL Alembic chain
(one template per module, copied per test). The real-PostgreSQL proof is
tests/integration/test_candidate_profile_transfer_postgres.py.

Every candidate value in the rich fixture contains SENTINEL so leak tests can
assert no candidate value reaches logs, errors or CLI output.
"""

import hashlib
import json
import logging
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db import candidate_profile_repository as repo
from app.db.models import (
    CandidateCertificationRecord,
    CandidateEducationRecord,
    CandidateExperienceRecord,
    CandidateJobPreferencesRecord,
    CandidateLanguageRecord,
    CandidateProfileRecord,
    CandidateProjectRecord,
    CandidateSkillRecord,
    JobRecord,
    UserProfile,
)
from app.models import candidate_profile as domain
from app.services import candidate_profile_transfer as transfer
from scripts import candidate_profile_transfer_stage13b as cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL13B"
HEAD = transfer.application_head()


# --- rich fixture (shared with the PostgreSQL integration module) ------------------


def rich_profile_record() -> CandidateProfileRecord:
    """Trusted, unconfirmed and inferred facts; a NON-EMPTY top-level fact
    (professional_title) with NO field_trust entry; dates, a current
    experience, incomplete education, certification, project, language and
    preferences. Child rows are added in a deliberately non-canonical order."""
    field_trust = {
        "first_name": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"},
        "last_name": {"source": "USER_CONFIRMED", "confidence": "CONFIRMED"},
        "location_city": {"source": "IMPORTED", "confidence": "UNCONFIRMED"},
        "professional_summary": {"source": "INFERRED", "confidence": "INFERRED"},
        "career_goal": {"source": "USER_PROVIDED_DOCUMENT", "confidence": "CONFIRMED"},
        "target_roles": {"source": "UNKNOWN", "confidence": "CONFIRMED"},
    }
    record = CandidateProfileRecord(
        id=1,
        profile_version=17,
        first_name=f"{SENTINEL}-First",
        last_name=f"{SENTINEL}-Last",
        professional_title=f"{SENTINEL}-Title-without-trust",
        location_city=f"{SENTINEL}-City",
        location_country=None,
        professional_summary=f"{SENTINEL} summary ✓ Ünïcode",
        career_goal=f"{SENTINEL} goal",
        target_roles_json=json.dumps([f"{SENTINEL}-Role-B", f"{SENTINEL}-Role-A"]),
        field_trust_json=json.dumps(field_trust),
    )
    record.skills = [
        CandidateSkillRecord(
            name=f"{SENTINEL}-Python",
            normalized_name=f"{SENTINEL}-python".casefold(),
            category="LANGUAGE",
            proficiency="ADVANCED",
            years_experience=4.5,
            last_used_year=2026,
            source="MANUAL_ENTRY",
            confidence="CONFIRMED",
            notes=f"{SENTINEL} note",
        ),
        CandidateSkillRecord(
            name=f"{SENTINEL}-Kubernetes",
            normalized_name=f"{SENTINEL}-kubernetes".casefold(),
            category="DEVOPS",
            proficiency="UNKNOWN",
            years_experience=None,
            last_used_year=None,
            source="INFERRED",
            confidence="UNCONFIRMED",
            notes=None,
        ),
        CandidateSkillRecord(
            name=f"{SENTINEL}-SQL",
            normalized_name=f"{SENTINEL}-sql".casefold(),
            category="DATABASE",
            proficiency="INTERMEDIATE",
            years_experience=2.0,
            last_used_year=2025,
            source="IMPORTED",
            confidence="CONFIRMED",
            notes=None,
        ),
    ]
    record.experiences = [
        CandidateExperienceRecord(
            company=f"{SENTINEL}-Employer-Now",
            job_title=f"{SENTINEL}-Engineer",
            start_date=date(2023, 4, 1),
            end_date=None,
            is_current=True,
            location=f"{SENTINEL}-Berlin",
            description=f"{SENTINEL} description",
            responsibilities_json=json.dumps([f"{SENTINEL}-r2", f"{SENTINEL}-r1"]),
            achievements_json=json.dumps([f"{SENTINEL}-a1"]),
            technologies_json=json.dumps([f"{SENTINEL}-t1", f"{SENTINEL}-t0"]),
            source="USER_CONFIRMED",
            confidence="CONFIRMED",
        ),
        CandidateExperienceRecord(
            company=f"{SENTINEL}-Employer-Old",
            job_title=f"{SENTINEL}-Intern",
            start_date=date(2019, 1, 1),
            end_date=date(2019, 6, 30),
            is_current=False,
            location=None,
            description=None,
            responsibilities_json="[]",
            achievements_json="[]",
            technologies_json="[]",
            source="INFERRED",
            confidence="INFERRED",
        ),
    ]
    record.education = [
        CandidateEducationRecord(
            institution=f"{SENTINEL}-University",
            program=f"{SENTINEL}-Program",
            degree=None,
            field_of_study=f"{SENTINEL}-CS",
            start_date=date(2018, 10, 1),
            end_date=None,
            completed=False,
            location=None,
            notes=f"{SENTINEL} incomplete",
            source="MANUAL_ENTRY",
            confidence="UNCONFIRMED",
        )
    ]
    record.certifications = [
        CandidateCertificationRecord(
            name=f"{SENTINEL}-Cert",
            issuer=f"{SENTINEL}-Issuer",
            issued_date=date(2024, 2, 29),
            expires_date=date(2027, 2, 28),
            credential_id=f"{SENTINEL}-CRED",
            credential_url=f"https://example.invalid/{SENTINEL}",
            status="COMPLETED",
            source="USER_PROVIDED_DOCUMENT",
            confidence="CONFIRMED",
        )
    ]
    record.projects = [
        CandidateProjectRecord(
            name=f"{SENTINEL}-Project",
            description=f"{SENTINEL} project",
            role=f"{SENTINEL}-Lead",
            technologies_json=json.dumps([f"{SENTINEL}-FastAPI"]),
            repository_url=f"https://example.invalid/{SENTINEL}/repo",
            demo_url=None,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 3, 1),
            highlights_json=json.dumps([f"{SENTINEL}-h1"]),
            source="UNKNOWN",
            confidence="UNKNOWN",
        )
    ]
    record.languages = [
        CandidateLanguageRecord(
            language=f"{SENTINEL}-German",
            normalized_language=f"{SENTINEL}-german".casefold(),
            level="B2",
            certificate=f"{SENTINEL}-Goethe",
            notes=None,
            source="MANUAL_ENTRY",
            confidence="CONFIRMED",
        ),
        CandidateLanguageRecord(
            language=f"{SENTINEL}-English",
            normalized_language=f"{SENTINEL}-english".casefold(),
            level="C1",
            certificate=None,
            notes=f"{SENTINEL} lang note",
            source="INFERRED",
            confidence="UNCONFIRMED",
        ),
    ]
    record.job_preferences = CandidateJobPreferencesRecord(
        preferred_locations_json=json.dumps([f"{SENTINEL}-Berlin", f"{SENTINEL}-Remote"]),
        remote_preference="HYBRID",
        employment_types_json=json.dumps(["FULL_TIME"]),
        minimum_salary=55000.0,
        salary_currency="EUR",
        relocation=None,
        travel=False,
    )
    return record


def seed_rich_profile(db: Session) -> None:
    db.add(rich_profile_record())
    db.commit()


# --- database helpers ---------------------------------------------------------------


@pytest.fixture(scope="module")
def template_db(tmp_path_factory) -> Path:
    from alembic.command import upgrade
    from alembic.config import Config

    path = tmp_path_factory.mktemp("s13b_template") / "template.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    upgrade(cfg, "head")
    return path


def _fresh(template_db: Path, path: Path) -> Path:
    shutil.copyfile(template_db, path)
    return path


def _session(path: Path) -> Session:
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    return Session(engine, autoflush=False)


@pytest.fixture()
def source_db(template_db, tmp_path) -> Path:
    path = _fresh(template_db, tmp_path / "source.db")
    with _session(path) as db:
        seed_rich_profile(db)
    return path


@pytest.fixture()
def target_db(template_db, tmp_path) -> Path:
    return _fresh(template_db, tmp_path / "target.db")


def _export(path: Path) -> transfer.TransferProfile:
    with _session(path) as db:
        return transfer.export_profile(db)


def _snapshot_bytes(profile: transfer.TransferProfile) -> bytes:
    return transfer.serialize_snapshot(
        transfer.build_snapshot(profile, source_dialect="sqlite", source_revision=HEAD)
    )


def _import(path: Path, snapshot: bytes) -> str:
    with _session(path) as db:
        return transfer.import_profile(db, transfer.parse_snapshot(snapshot))


def _raw_profile_row(path: Path) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM candidate_profiles").fetchone())
    finally:
        conn.close()


def _domain_counts(path: Path) -> dict[str, int]:
    with _session(path) as db:
        return transfer.profile_domain_counts(db)


def _tamper(snapshot: bytes, mutate) -> bytes:
    document = json.loads(snapshot)
    mutate(document)
    return json.dumps(document).encode("utf-8")


def _rehash(document: dict) -> None:
    profile = transfer.TransferProfile.model_validate_json(json.dumps(document["profile"]))
    document["semantic_digest"] = transfer.semantic_digest(profile)


# --- coverage guard -----------------------------------------------------------------


class TestTransferCoversTheWholeDomain:
    """A field added to the CandidateProfile domain must be added to the
    transfer format too -- otherwise transfer would silently drop it."""

    @pytest.mark.parametrize(
        ("transfer_model", "domain_model"),
        [
            (transfer.TransferSkill, domain.CandidateSkill),
            (transfer.TransferExperience, domain.CandidateExperience),
            (transfer.TransferEducation, domain.CandidateEducation),
            (transfer.TransferCertification, domain.CandidateCertification),
            (transfer.TransferProject, domain.CandidateProject),
            (transfer.TransferLanguage, domain.CandidateLanguage),
            (transfer.TransferJobPreferences, domain.CandidateJobPreferences),
            (transfer.TransferFieldTrust, domain.FieldTrust),
        ],
    )
    def test_nested_models_match_domain_minus_ids(self, transfer_model, domain_model):
        expected = set(domain_model.model_fields) - transfer.NON_SEMANTIC_ITEM_FIELDS
        assert set(transfer_model.model_fields) == expected

    def test_profile_model_matches_domain_minus_persistence_metadata(self):
        expected = set(domain.CandidateProfile.model_fields) - transfer.NON_SEMANTIC_PROFILE_FIELDS
        assert set(transfer.TransferProfile.model_fields) == expected

    def test_snapshot_models_have_no_defaults(self):
        models = [transfer.TransferProfile, transfer.TransferFieldTrust] + [
            spec[0] for spec in transfer._COLLECTION_SPECS.values()
        ]
        for model in [*models, transfer.TransferJobPreferences]:
            for name, field in model.model_fields.items():
                assert field.is_required(), f"{model.__name__}.{name} must be required"

    def test_profile_domain_tables_cover_every_candidate_profile_relationship(self):
        relationships = {
            rel.mapper.local_table.name for rel in CandidateProfileRecord.__mapper__.relationships
        }
        assert relationships | {"candidate_profiles"} == set(transfer.PROFILE_DOMAIN_TABLES)

    def test_historical_tables_are_every_other_application_table(self):
        tables = set(transfer.historical_state_tables())
        assert tables.isdisjoint(transfer.PROFILE_DOMAIN_TABLES)
        for expected in (
            "jobs",
            "automation_runs",
            "gmail_messages",
            "gmail_threads",
            "gmail_message_analyses",
            "response_drafts",
            "response_draft_approvals",
            "response_draft_sends",
            "follow_up_proposals",
            "follow_up_approvals",
            "follow_up_sends",
            "application_package_reviews",
            "gmail_application_drafts",
            "telegram_bewerbung_approvals",
            "telegram_vacancy_reviews",
            "xing_scan_progress",
            "automation_mail_progress",
            "automation_schedules",
            "user_profiles",
        ):
            assert expected in tables


# --- export -------------------------------------------------------------------------


class TestExport:
    def test_missing_profile_fails_and_creates_nothing(self, target_db):
        with _session(target_db) as db:
            with pytest.raises(transfer.SourceProfileMissingError):
                transfer.export_profile(db)
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_export_is_deterministic_across_runs_and_exported_at(self, source_db):
        first, second = _export(source_db), _export(source_db)
        assert transfer.semantic_digest(first) == transfer.semantic_digest(second)
        a = transfer.build_snapshot(first, source_dialect="sqlite", source_revision=HEAD)
        b = transfer.build_snapshot(second, source_dialect="sqlite", source_revision=HEAD)
        b["exported_at"] = "1999-01-01T00:00:00+00:00"
        assert a["semantic_digest"] == b["semantic_digest"]

    def test_top_level_trust_preserved_and_absence_stays_absent(self, source_db):
        profile = _export(source_db)
        assert profile.professional_title == f"{SENTINEL}-Title-without-trust"
        assert "professional_title" not in profile.field_trust
        assert "location_country" not in profile.field_trust
        assert profile.field_trust["location_city"].model_dump() == {
            "source": "IMPORTED",
            "confidence": "UNCONFIRMED",
        }
        assert profile.field_trust["professional_summary"].source == "INFERRED"
        assert profile.field_trust["professional_summary"].confidence == "INFERRED"
        assert len(profile.field_trust) == 6

    def test_nested_provenance_preserved(self, source_db):
        profile = _export(source_db)
        skills = {s.name: (s.source, s.confidence) for s in profile.skills}
        assert skills[f"{SENTINEL}-Kubernetes"] == ("INFERRED", "UNCONFIRMED")
        assert skills[f"{SENTINEL}-SQL"] == ("IMPORTED", "CONFIRMED")
        assert {(e.source, e.confidence) for e in profile.experiences} == {
            ("USER_CONFIRMED", "CONFIRMED"),
            ("INFERRED", "INFERRED"),
        }
        assert profile.projects[0].source == "UNKNOWN"
        assert profile.education[0].confidence == "UNCONFIRMED"

    def test_child_ids_and_profile_version_are_not_semantic(self, source_db, template_db, tmp_path):
        other = _fresh(template_db, tmp_path / "other.db")
        record = rich_profile_record()
        record.profile_version = 1
        record.skills = list(reversed(record.skills))
        record.languages = list(reversed(record.languages))
        with _session(other) as db:
            db.add(
                CandidateSkillRecord(  # burn ids so child ids differ
                    candidate_profile_id=1,
                    name="x",
                    normalized_name="x",
                    source="UNKNOWN",
                    confidence="UNKNOWN",
                )
            )
            db.flush()
            db.execute(text("DELETE FROM candidate_skills"))
            db.add(record)
            db.commit()
        exported = _export(source_db).model_dump(mode="json")
        assert "id" not in json.dumps(list(exported)) and "profile_version" not in exported
        for collection in transfer.COLLECTIONS:
            assert all("id" not in item for item in exported[collection])
        assert transfer.semantic_digest(_export(other)) == transfer.semantic_digest(
            _export(source_db)
        )

    def test_export_never_writes(self, source_db):
        before = hashlib.sha256(source_db.read_bytes()).hexdigest()
        with _session(source_db) as db:
            transfer.export_profile(db)
            assert not db.new and not db.dirty and not db.deleted
        assert hashlib.sha256(source_db.read_bytes()).hexdigest() == before

    def test_export_through_read_only_engine_leaves_file_unchanged(self, source_db):
        before = hashlib.sha256(source_db.read_bytes()).hexdigest()
        with cli.session_for(cli.read_only_engine(f"sqlite:///{source_db}")) as db:
            transfer.export_profile(db)
        assert hashlib.sha256(source_db.read_bytes()).hexdigest() == before

    def test_partial_field_trust_entry_is_refused_not_upgraded(self, source_db):
        conn = sqlite3.connect(source_db)
        conn.execute(
            "UPDATE candidate_profiles SET field_trust_json = ?",
            (json.dumps({"first_name": {"source": "INFERRED"}}),),
        )
        conn.commit()
        conn.close()
        # The application's lenient loader WOULD upgrade this to CONFIRMED:
        with _session(source_db) as db:
            lenient = repo.to_candidate_profile_response(repo.get_candidate_profile(db))
        assert lenient.field_trust["first_name"].confidence == "CONFIRMED"
        with pytest.raises(transfer.SourceProfileInvalidError):
            _export(source_db)

    def test_unknown_field_trust_key_is_refused(self, source_db):
        conn = sqlite3.connect(source_db)
        conn.execute(
            "UPDATE candidate_profiles SET field_trust_json = ?",
            (json.dumps({"skills": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"}}),),
        )
        conn.commit()
        conn.close()
        with pytest.raises(transfer.SourceProfileInvalidError):
            _export(source_db)

    @staticmethod
    def _set_raw_field_trust(source_db: Path, raw: str) -> None:
        conn = sqlite3.connect(source_db)
        conn.execute("UPDATE candidate_profiles SET field_trust_json = ?", (raw,))
        conn.commit()
        conn.close()

    # Hand-written JSON: json.dumps can never emit a duplicate key.
    _DUPLICATE_TOP_LEVEL = (
        '{"professional_title": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"},'
        ' "professional_title": {"source": "INFERRED", "confidence": "INFERRED"}}'
    )

    def test_duplicate_top_level_field_trust_key_is_refused(self, source_db):
        self._set_raw_field_trust(source_db, self._DUPLICATE_TOP_LEVEL)
        # Plain json.loads would silently keep only the LAST entry:
        assert json.loads(self._DUPLICATE_TOP_LEVEL)["professional_title"]["source"] == "INFERRED"
        with pytest.raises(transfer.SourceProfileInvalidError, match="duplicate object key"):
            _export(source_db)

    def test_duplicate_key_inside_a_trust_entry_is_refused(self, source_db):
        self._set_raw_field_trust(
            source_db,
            '{"first_name": {"source": "MANUAL_ENTRY", "source": "INFERRED",'
            ' "confidence": "CONFIRMED"}}',
        )
        with pytest.raises(transfer.SourceProfileInvalidError, match="duplicate object key"):
            _export(source_db)

    def test_valid_unique_field_trust_map_is_accepted_unchanged(self, source_db):
        trust = {
            "professional_title": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"},
            "location_city": {"source": "IMPORTED", "confidence": "UNCONFIRMED"},
        }
        self._set_raw_field_trust(source_db, json.dumps(trust))
        profile = _export(source_db)
        assert {k: v.model_dump() for k, v in profile.field_trust.items()} == trust

    def test_partial_entry_alongside_unique_keys_is_still_refused(self, source_db):
        self._set_raw_field_trust(
            source_db,
            json.dumps(
                {
                    "first_name": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"},
                    "last_name": {"confidence": "CONFIRMED"},
                }
            ),
        )
        with pytest.raises(transfer.SourceProfileInvalidError):
            _export(source_db)

    def test_duplicate_key_error_leaks_no_candidate_value(self, source_db, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        self._set_raw_field_trust(
            source_db,
            '{"first_name": {"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"},'
            f' "first_name": {{"source": "{SENTINEL}-dup", "confidence": "{SENTINEL}-dup"}}}}',
        )
        with pytest.raises(transfer.SourceProfileInvalidError) as excinfo:
            _export(source_db)
        assert SENTINEL not in str(excinfo.value)
        assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
        captured = capsys.readouterr()
        assert SENTINEL not in caplog.text + captured.out + captured.err

    def test_source_not_at_head_is_refused(self, source_db):
        conn = sqlite3.connect(source_db)
        conn.execute("UPDATE alembic_version SET version_num = 'e8b1c4f7a2d9'")
        conn.commit()
        conn.close()
        with pytest.raises(transfer.SchemaRevisionMismatchError):
            _export(source_db)

    def test_snapshot_contains_no_config_or_secrets(self, source_db, monkeypatch):
        secret = "s13b-secret-value"
        for name in ("DATABASE_URL", "API_KEY", "TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD"):
            monkeypatch.setenv(name, f"{name}-{secret}")
        data = _snapshot_bytes(_export(source_db))
        assert secret.encode() not in data
        document = json.loads(data)
        assert set(document) == {
            "format",
            "format_version",
            "exported_at",
            "source",
            "semantic_digest",
            "profile",
        }
        assert document["source"] == {"dialect": "sqlite", "alembic_revision": HEAD}
        assert str(source_db).encode() not in data

    def test_legacy_user_profile_is_never_a_source(self, source_db):
        digest = transfer.semantic_digest(_export(source_db))
        with _session(source_db) as db:
            db.add(UserProfile(name=f"{SENTINEL}-legacy", skills_json='["LegacySkill"]'))
            db.commit()
        assert transfer.semantic_digest(_export(source_db)) == digest
        source = (PROJECT_ROOT / "app/services/candidate_profile_transfer.py").read_text("utf-8")
        assert "UserProfile" not in source and "user_profiles" not in source


# --- snapshot -----------------------------------------------------------------------


class TestSnapshot:
    @pytest.fixture()
    def snapshot(self, source_db) -> bytes:
        return _snapshot_bytes(_export(source_db))

    def test_valid_snapshot_round_trips(self, snapshot, source_db):
        parsed = transfer.parse_snapshot(snapshot)
        assert parsed.digest == transfer.semantic_digest(_export(source_db))
        assert parsed.source_revision == HEAD

    def test_canonical_json_is_deterministic(self):
        a = transfer.canonical_json({"b": [1, {"y": 2, "x": "ü"}], "a": None})
        b = transfer.canonical_json({"a": None, "b": [1, {"x": "ü", "y": 2}]})
        assert a == b == '{"a":null,"b":[1,{"x":"ü","y":2}]}'

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d["profile"].update(first_name="X"), id="scalar"),
            pytest.param(
                lambda d: d["profile"]["field_trust"]["location_city"].update(
                    confidence="CONFIRMED"
                ),
                id="field-trust-confidence",
            ),
            pytest.param(
                lambda d: d["profile"]["field_trust"].update(
                    professional_title={"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"}
                ),
                id="added-trust-entry",
            ),
            pytest.param(
                lambda d: d["profile"]["skills"][0].update(source="MANUAL_ENTRY"),
                id="nested-source",
            ),
            pytest.param(
                lambda d: d["profile"]["job_preferences"].update(travel=True), id="preferences"
            ),
        ],
    )
    def test_any_semantic_change_breaks_the_digest(self, snapshot, mutate):
        with pytest.raises(transfer.SnapshotDigestMismatchError):
            transfer.parse_snapshot(_tamper(snapshot, mutate))

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d["profile"].pop("career_goal"), id="missing-field"),
            pytest.param(
                lambda d: d["profile"]["skills"][0].pop("confidence"), id="missing-confidence"
            ),
            pytest.param(
                lambda d: d["profile"]["field_trust"]["first_name"].pop("source"),
                id="missing-trust-source",
            ),
            pytest.param(lambda d: d["profile"].update(extra="x"), id="extra-field"),
            pytest.param(lambda d: d.update(format_version=2), id="unsupported-version"),
            pytest.param(lambda d: d.update(format_version="1"), id="string-version"),
            pytest.param(lambda d: d.update(format="other"), id="wrong-format"),
            pytest.param(lambda d: d.pop("semantic_digest"), id="missing-digest"),
            pytest.param(lambda d: d.update(unexpected=1), id="extra-top-level-key"),
            pytest.param(
                lambda d: d["profile"]["skills"][0].update(last_used_year="2026"),
                id="no-type-coercion",
            ),
            pytest.param(
                lambda d: d["profile"]["skills"][0].update(source="TRUSTED"), id="bad-literal"
            ),
            pytest.param(
                lambda d: d["profile"]["field_trust"].update(
                    skills={"source": "MANUAL_ENTRY", "confidence": "CONFIRMED"}
                ),
                id="non-top-level-trust-key",
            ),
            pytest.param(lambda d: d["profile"]["skills"].reverse(), id="non-canonical-order"),
            pytest.param(
                lambda d: d["profile"]["skills"][0].update(name=" padded "),
                id="non-canonical-domain-form",
            ),
            pytest.param(
                lambda d: d["profile"]["experiences"][0].update(
                    start_date="2020-01-01", end_date="2019-01-01"
                ),
                id="domain-date-order",
            ),
            pytest.param(lambda d: d["profile"].update(first_name="x" * 101), id="column-length"),
        ],
    )
    def test_structurally_invalid_snapshot_is_rejected(self, snapshot, mutate):
        def mutate_and_rehash(document):
            mutate(document)
            if "semantic_digest" not in document:
                return
            try:
                _rehash(document)
            except Exception:  # noqa: BLE001 -- unhashable on purpose: still must be rejected
                pass

        with pytest.raises(transfer.SnapshotInvalidError):
            transfer.parse_snapshot(_tamper(snapshot, mutate_and_rehash))

    @pytest.mark.parametrize(
        ("collection", "field"),
        [("skills", "name"), ("languages", "language"), ("projects", "name")],
    )
    def test_duplicate_normalized_entries_are_rejected(self, snapshot, collection, field):
        def duplicate(document):
            items = document["profile"][collection]
            clone = dict(items[0])
            clone[field] = f"  {items[0][field].upper()} ".strip()
            if collection == "skills":
                clone["notes"] = "different"
            items.append(clone)
            items.sort(key=transfer.canonical_json)
            _rehash(document)

        with pytest.raises(transfer.SnapshotInvalidError, match="duplicate"):
            transfer.parse_snapshot(_tamper(snapshot, duplicate))

    @pytest.mark.parametrize(
        "data",
        [
            b"{not json",
            b"\xff\xfe",
            b"[]",
            b'{"format": 1, "format": 2}',
            b'{"x": NaN}',
        ],
        ids=["malformed", "not-utf8", "not-object", "duplicate-key", "nan"],
    )
    def test_malformed_json_is_rejected(self, data):
        with pytest.raises(transfer.SnapshotInvalidError):
            transfer.parse_snapshot(data)

    def test_validation_errors_never_echo_candidate_values(self, snapshot):
        def bad(document):
            document["profile"]["skills"][0]["source"] = f"{SENTINEL}-not-a-source"
            document["profile"]["field_trust"][f"{SENTINEL}-key"] = {"source": "x"}

        with pytest.raises(transfer.SnapshotInvalidError) as excinfo:
            transfer.parse_snapshot(_tamper(snapshot, bad))
        assert SENTINEL not in str(excinfo.value)


# --- import -------------------------------------------------------------------------


class TestImport:
    @pytest.fixture()
    def snapshot(self, source_db) -> bytes:
        return _snapshot_bytes(_export(source_db))

    def test_import_into_empty_destination(self, snapshot, source_db, target_db):
        digest = _import(target_db, snapshot)
        assert digest == transfer.parse_snapshot(snapshot).digest
        assert transfer.semantic_digest(_export(target_db)) == digest
        assert _raw_profile_row(target_db)["profile_version"] == transfer.FRESH_PROFILE_VERSION
        assert transfer.compare_profiles(_export(source_db), _export(target_db)) == []

    def test_absent_trust_stays_absent_and_nested_trust_is_exact(self, snapshot, target_db):
        _import(target_db, snapshot)
        stored = json.loads(_raw_profile_row(target_db)["field_trust_json"])
        assert "professional_title" not in stored
        assert stored["location_city"] == {"source": "IMPORTED", "confidence": "UNCONFIRMED"}
        assert stored["professional_summary"] == {"source": "INFERRED", "confidence": "INFERRED"}
        conn = sqlite3.connect(target_db)
        rows = dict(conn.execute("SELECT name, source || '/' || confidence FROM candidate_skills"))
        conn.close()
        assert rows[f"{SENTINEL}-Kubernetes"] == "INFERRED/UNCONFIRMED"
        assert rows[f"{SENTINEL}-Python"] == "MANUAL_ENTRY/CONFIRMED"
        assert rows[f"{SENTINEL}-SQL"] == "IMPORTED/CONFIRMED"

    def test_generation_and_scoring_trust_equivalence(self, snapshot, source_db, target_db):
        _import(target_db, snapshot)
        with _session(source_db) as s, _session(target_db) as t:
            assert (
                repo.get_candidate_skills_for_scoring(s)
                == repo.get_candidate_skills_for_scoring(t)
                == frozenset({f"{SENTINEL}-Python"})
            )
            src = transfer.read_profile_response(s)
            dst = transfer.read_profile_response(t)
            for name in domain.TOP_LEVEL_TRUST_FIELDS:
                assert domain.is_top_level_fact_usable_for_generation(
                    src, name
                ) == domain.is_top_level_fact_usable_for_generation(dst, name)
            assert (
                domain.is_top_level_fact_usable_for_generation(dst, "professional_title") is False
            )
            assert transfer.trust_decisions(src) == transfer.trust_decisions(dst)

    def test_no_historical_state_is_created(self, snapshot, target_db):
        _import(target_db, snapshot)
        with _session(target_db) as db:
            counts = transfer.historical_state_counts(db)
        assert counts and all(count == 0 for count in counts.values())

    def test_existing_profile_is_refused_without_mutation(self, snapshot, target_db):
        with _session(target_db) as db:
            repo.get_or_create_candidate_profile(db)
        before = hashlib.sha256(target_db.read_bytes()).hexdigest()
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(target_db, snapshot)
        assert hashlib.sha256(target_db.read_bytes()).hexdigest() == before

    def test_second_import_is_refused_without_mutation(self, snapshot, target_db):
        _import(target_db, snapshot)
        before = hashlib.sha256(target_db.read_bytes()).hexdigest()
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(target_db, snapshot)
        assert hashlib.sha256(target_db.read_bytes()).hexdigest() == before

    def test_orphan_profile_child_rows_are_refused(self, snapshot, target_db):
        conn = sqlite3.connect(target_db)  # SQLite does not enforce the FK by default
        conn.execute(
            "INSERT INTO candidate_languages (candidate_profile_id, language, "
            "normalized_language, level, source, confidence) "
            "VALUES (1, 'x', 'x', 'A1', 'UNKNOWN', 'UNKNOWN')"
        )
        conn.commit()
        conn.close()
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(target_db, snapshot)
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_destination_with_operational_state_is_refused(self, snapshot, target_db):
        with _session(target_db) as db:
            db.add(
                JobRecord(
                    fingerprint="f" * 64,
                    title="t",
                    company="c",
                    location="l",
                    url="https://example.invalid/job",
                    source="test",
                    score=0,
                    recommendation="SKIP",
                )
            )
            db.commit()
        with pytest.raises(transfer.DestinationNotEmptyError):
            _import(target_db, snapshot)
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_corrupted_digest_is_refused_before_any_write(self, snapshot, target_db):
        bad = _tamper(snapshot, lambda d: d.update(semantic_digest="0" * 64))
        before = hashlib.sha256(target_db.read_bytes()).hexdigest()
        with pytest.raises(transfer.SnapshotDigestMismatchError):
            _import(target_db, bad)
        assert hashlib.sha256(target_db.read_bytes()).hexdigest() == before

    def test_destination_not_at_head_is_refused_before_any_write(self, snapshot, target_db):
        conn = sqlite3.connect(target_db)
        conn.execute("UPDATE alembic_version SET version_num = 'e8b1c4f7a2d9'")
        conn.commit()
        conn.close()
        with pytest.raises(transfer.SchemaRevisionMismatchError):
            _import(target_db, snapshot)
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_snapshot_from_another_revision_is_refused(self, snapshot, target_db):
        def other_revision(document):
            document["source"]["alembic_revision"] = "e8b1c4f7a2d9"

        with pytest.raises(transfer.SchemaRevisionMismatchError):
            _import(target_db, _tamper(snapshot, other_revision))
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_flush_failure_rolls_back_everything(self, snapshot, target_db, monkeypatch):
        original = transfer.build_records

        def broken(profile):
            record = original(profile)
            record.languages[-1].normalized_language = None  # NOT NULL violation at flush
            return record

        monkeypatch.setattr(transfer, "build_records", broken)
        with pytest.raises(Exception):  # noqa: B017 -- the DB error class is dialect-specific
            _import(target_db, snapshot)
        assert set(_domain_counts(target_db).values()) == {0}

    def test_verification_failure_rolls_back_everything(self, snapshot, target_db, monkeypatch):
        real = transfer.payload_from_record

        def drifted(record):
            profile = real(record)
            return profile.model_copy(update={"career_goal": "drifted"})

        monkeypatch.setattr(transfer, "payload_from_record", drifted)
        with pytest.raises(transfer.ImportVerificationError):
            _import(target_db, snapshot)
        assert set(_domain_counts(target_db).values()) == {0}

    def test_import_does_not_rely_on_patch_or_trust_defaults(self):
        source = (PROJECT_ROOT / "app/services/candidate_profile_transfer.py").read_text("utf-8")
        assert "apply_candidate_profile_patch" not in source
        assert "get_or_create_candidate_profile" not in source
        assert "FieldTrust()" not in source

    def test_job_preferences_absence_is_preserved(self, template_db, tmp_path):
        source = _fresh(template_db, tmp_path / "nopref-src.db")
        target = _fresh(template_db, tmp_path / "nopref-dst.db")
        record = rich_profile_record()
        record.job_preferences = None
        with _session(source) as db:
            db.add(record)
            db.commit()
        _import(target, _snapshot_bytes(_export(source)))
        assert _export(target).job_preferences is None
        assert _domain_counts(target)["candidate_job_preferences"] == 0


class TestCompare:
    def test_compare_reports_paths_never_values(self, source_db):
        profile = _export(source_db)
        changed = profile.model_copy(
            update={
                "first_name": "other",
                "field_trust": {
                    **profile.field_trust,
                    "location_city": transfer.TransferFieldTrust(
                        source="IMPORTED", confidence="CONFIRMED"
                    ),
                },
            }
        )
        mismatches = transfer.compare_profiles(profile, changed)
        assert mismatches == ["first_name", "field_trust.location_city.confidence"]
        assert all(SENTINEL not in m for m in mismatches)

    def test_compare_databases_passes_after_import(self, source_db, target_db):
        _import(target_db, _snapshot_bytes(_export(source_db)))
        summary = cli.compare_databases(f"sqlite:///{source_db}", f"sqlite:///{target_db}")
        assert summary["status"] == "PASS", summary["checks"]
        assert summary["source_semantic_digest"] == summary["target_semantic_digest"]
        assert SENTINEL not in json.dumps(summary)

    def test_compare_databases_detects_drift(self, source_db, target_db):
        _import(target_db, _snapshot_bytes(_export(source_db)))
        conn = sqlite3.connect(target_db)
        conn.execute(
            "UPDATE candidate_skills SET source = 'MANUAL_ENTRY', confidence = 'CONFIRMED'"
        )
        conn.commit()
        conn.close()
        summary = cli.compare_databases(f"sqlite:///{source_db}", f"sqlite:///{target_db}")
        assert summary["status"] == "FAIL"
        assert not summary["checks"]["semantic_digest_equal"]
        assert not summary["checks"]["typed_semantic_equal"]
        assert not summary["checks"]["scoring_skill_projection_equal"]
        assert not summary["checks"]["generation_trust_equal"]


# --- CLI safety ---------------------------------------------------------------------


class TestCliSafety:
    def test_no_escape_hatch_flags_exist(self):
        for flag in ("--force", "--overwrite", "--ignore-digest", "--skip-provenance"):
            with pytest.raises(SystemExit):
                cli.parse_args(["import", "--snapshot", "x.json", flag])

    def test_export_refuses_a_path_inside_the_repository(self, source_db, monkeypatch, capsys):
        monkeypatch.setenv(cli.SOURCE_URL_ENV, f"sqlite:///{source_db}")
        target = PROJECT_ROOT / "snapshot-must-not-exist.json"
        assert cli.main(["export", "--out", str(target)]) == 2
        assert not target.exists()
        assert "outside the repository" in capsys.readouterr().err

    def test_export_writes_new_file_and_never_overwrites(
        self, source_db, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv(cli.SOURCE_URL_ENV, f"sqlite:///{source_db}")
        out = tmp_path / "snap.json"
        assert cli.main(["export", "--out", str(out)]) == 0
        printed = capsys.readouterr().out
        assert SENTINEL not in printed
        summary = json.loads(printed)
        assert summary["semantic_digest"] == transfer.parse_snapshot(out.read_bytes()).digest
        content = out.read_bytes()
        assert cli.main(["export", "--out", str(out)]) == 2
        assert out.read_bytes() == content
        assert cli.main(["verify", "--snapshot", str(out)]) == 0

    def test_export_missing_profile_fails_closed(self, target_db, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(cli.SOURCE_URL_ENV, f"sqlite:///{target_db}")
        out = tmp_path / "snap.json"
        assert cli.main(["export", "--out", str(out)]) == 2
        assert "category=source_profile_missing" in capsys.readouterr().err
        assert not out.exists()
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_export_of_missing_sqlite_file_never_creates_it(self, tmp_path, monkeypatch):
        missing = tmp_path / "missing.db"
        monkeypatch.setenv(cli.SOURCE_URL_ENV, f"sqlite:///{missing}")
        assert cli.main(["export", "--out", str(tmp_path / "s.json")]) == 2
        assert not missing.exists()

    def test_import_requires_postgresql(self, source_db, target_db, tmp_path, monkeypatch, capsys):
        snap = tmp_path / "s.json"
        snap.write_bytes(_snapshot_bytes(_export(source_db)))
        monkeypatch.delenv(cli.SOURCE_URL_ENV, raising=False)
        monkeypatch.setenv(cli.TARGET_URL_ENV, f"sqlite:///{target_db}")
        assert cli.main(["import", "--snapshot", str(snap)]) == 2
        assert "must be PostgreSQL" in capsys.readouterr().err
        assert _domain_counts(target_db)["candidate_profiles"] == 0

    def test_source_and_target_must_be_distinct(self, source_db, monkeypatch, capsys):
        monkeypatch.setenv(cli.SOURCE_URL_ENV, f"sqlite:///{source_db}")
        monkeypatch.setenv(cli.TARGET_URL_ENV, f"sqlite:///{source_db}")
        assert cli.main(["compare"]) == 2
        assert "distinct" in capsys.readouterr().err
        assert cli._same_database(
            "postgresql+psycopg://a:p1@h:5432/db", "postgresql://b:p2@h:5432/db"
        )
        assert not cli._same_database(
            "postgresql+psycopg://a:p@h:5432/db1", "postgresql+psycopg://a:p@h:5432/db2"
        )

    def test_unexpected_errors_print_only_the_error_type(self, monkeypatch, capsys):
        def explode(_args):
            raise RuntimeError(f"{SENTINEL} postgresql://u:secretpw@h/db")

        monkeypatch.setattr(cli, "run", explode)
        assert cli.main(["compare"]) == 1
        captured = capsys.readouterr()
        assert SENTINEL not in captured.err + captured.out
        assert "secretpw" not in captured.err + captured.out
        assert "error_type=RuntimeError" in captured.err

    def test_no_candidate_value_reaches_logs(self, source_db, target_db, caplog):
        caplog.set_level(logging.DEBUG)
        snapshot = _snapshot_bytes(_export(source_db))
        _import(target_db, snapshot)
        cli.compare_databases(f"sqlite:///{source_db}", f"sqlite:///{target_db}")
        assert "candidate_profile_transfer_imported" in caplog.text
        assert SENTINEL not in caplog.text


# --- SQLite archive -----------------------------------------------------------------


class TestSqliteArchive:
    def test_archive_is_byte_identical_read_only_and_validated(self, source_db, tmp_path):
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        source_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()
        source_mtime = source_db.stat().st_mtime_ns
        summary = cli.archive_sqlite(source_db, archive_dir)
        archive = Path(summary["archive_path"])
        assert summary["status"] == "PASS"
        assert summary["source_sha256"] == summary["archive_sha256"] == source_sha
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == source_sha
        assert summary["integrity_check"] == "ok"
        assert summary["alembic_revisions"] == [HEAD]
        assert summary["candidate_profile_count"] == 1
        assert summary["table_counts"]["candidate_skills"] == 3
        assert source_db.stat().st_mtime_ns == source_mtime
        assert not any(Path(f"{archive}{s}").exists() for s in cli.SQLITE_SIDECARS)
        with pytest.raises(PermissionError):
            archive.open("ab")
        assert SENTINEL not in json.dumps(summary)

    def test_archive_without_profile_reports_fail(self, target_db, tmp_path):
        summary = cli.archive_sqlite(target_db, tmp_path)
        assert summary["status"] == "FAIL"
        assert summary["problems"] == ["candidate_profile_count"]

    @pytest.mark.parametrize("suffix", ["-wal", "-journal"])
    def test_sidecar_is_refused_not_checkpointed(self, source_db, tmp_path, suffix):
        sidecar = Path(f"{source_db}{suffix}")
        sidecar.write_bytes(b"")
        with pytest.raises(cli.ArchiveError):
            cli.archive_sqlite(source_db, tmp_path)
        assert sidecar.exists()

    def test_wal_mode_source_is_refused(self, source_db, tmp_path):
        conn = sqlite3.connect(source_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()  # last connection closed: checkpointed, no -wal left
        archive_dir = tmp_path / "out"
        archive_dir.mkdir()
        with pytest.raises(cli.ArchiveError, match="WAL"):
            cli.archive_sqlite(source_db, archive_dir)
        assert list(archive_dir.iterdir()) == []

    def test_archive_dir_inside_repository_is_refused(self, source_db):
        with pytest.raises(cli.CliError):
            cli.archive_sqlite(source_db, PROJECT_ROOT / "backups")
