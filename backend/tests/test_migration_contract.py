from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.database import Base


BACKEND_ROOT = Path(__file__).resolve().parents[1]
INITIAL_REVISION = BACKEND_ROOT / "migrations" / "versions" / "20260804_0001_v6_initial.py"
ATTRIBUTION_REVISION = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260808_0002_evidence_attribution.py"
)


def _alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "AUTO_CREATE_DB": "false",
        "MODEL_GATEWAY_MODE": "mock",
        "DEEPSEEK_API_KEY": "",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_frozen_initial_migration_recreates_current_schema(tmp_path: Path) -> None:
    migration_source = INITIAL_REVISION.read_text(encoding="utf-8")
    assert "Base.metadata" not in migration_source
    assert "op.create_table" in migration_source
    assert "op.drop_table" in migration_source

    database_path = tmp_path / "fresh-v6.db"
    database_url = f"sqlite:///{database_path}"
    _alembic(database_url, "upgrade", "head")
    current = _alembic(database_url, "current")
    assert "20260808_0002 (head)" in current.stdout
    no_drift = _alembic(database_url, "check")
    assert "No new upgrade operations detected" in no_drift.stdout

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert table_names == {*Base.metadata.tables, "alembic_version"}

    _alembic(database_url, "downgrade", "base")
    with sqlite3.connect(database_path) as connection:
        remaining_application_tables = connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT IN ('alembic_version', 'sqlite_sequence')"
        ).fetchone()[0]
    assert remaining_application_tables == 0


def test_attribution_migration_keeps_legacy_evidence_unclassified(
    tmp_path: Path,
) -> None:
    migration_source = ATTRIBUTION_REVISION.read_text(encoding="utf-8")
    assert "participant_owned" in migration_source
    assert "legacy_unclassified" in migration_source
    assert "UPDATE evidence_items" not in migration_source

    database_path = tmp_path / "legacy-evidence.db"
    database_url = f"sqlite:///{database_path}"
    _alembic(database_url, "upgrade", "20260808_0001")

    timestamp = "2026-08-08 00:00:00"
    fingerprint = "a" * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO assessment_sessions (
                id, uuid, phase, consent_version, consent_accepted_at,
                profile_data, user_answer_count, finalization_state,
                ended_early, manual_review_recommended, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "00000000-0000-0000-0000-000000000001",
                "completed",
                "consent-v1",
                timestamp,
                "{}",
                1,
                "completed",
                0,
                0,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO dialogue_turns (
                id, session_id, turn_index, role, phase, content,
                quality_flags, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, 1, "user", "completed", "这是旧版用户输入。", "[]", timestamp),
        )
        connection.execute(
            """
            INSERT INTO scoring_runs (
                id, session_id, attempt_number, status, transcript_fingerprint,
                model_provider, model_name, prompt_template_id, prompt_version,
                repair_used, manual_review_recommended, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "completed",
                fingerprint,
                "mock",
                "mock",
                "final_scorer_v6.2.0",
                "v6.2.0",
                0,
                0,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_items (
                id, session_id, scoring_run_id, user_turn_id, dimension_key,
                quote, quote_start, quote_end, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                1,
                "problem_definition",
                "旧版用户输入",
                2,
                8,
                0.8,
                timestamp,
            ),
        )
        connection.commit()

    _alembic(database_url, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            """
            SELECT attribution_span_id, readiness_check_id,
                   validation_status, validation_reason
            FROM evidence_items WHERE id = 1
            """
        ).fetchone()
        attribution_count = connection.execute(
            "SELECT count(*) FROM evidence_attribution_spans"
        ).fetchone()[0]
        assert migrated == (None, None, "legacy_unclassified", None)
        assert attribution_count == 0

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE evidence_items SET validation_status = 'validated' WHERE id = 1"
            )

    _alembic(database_url, "downgrade", "20260808_0001")
    with sqlite3.connect(database_path) as connection:
        evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(evidence_items)")
        }
        attribution_table = connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'evidence_attribution_spans'"
        ).fetchone()[0]
        evidence_count = connection.execute(
            "SELECT count(*) FROM evidence_items"
        ).fetchone()[0]

    assert "attribution_span_id" not in evidence_columns
    assert "readiness_check_id" not in evidence_columns
    assert "validation_status" not in evidence_columns
    assert "validation_reason" not in evidence_columns
    assert attribution_table == 0
    assert evidence_count == 1
