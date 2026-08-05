from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.core.database import Base


BACKEND_ROOT = Path(__file__).resolve().parents[1]
INITIAL_REVISION = BACKEND_ROOT / "migrations" / "versions" / "20260804_0001_v6_initial.py"
PROVENANCE_REVISION = "20260804_0002"
CONTRACT_SHA_REVISION = "20260804_0003"


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
    assert f"{CONTRACT_SHA_REVISION} (head)" in current.stdout
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


def test_model_provenance_migration_upgrades_and_downgrades_cleanly(tmp_path: Path) -> None:
    database_path = tmp_path / "provenance-upgrade.db"
    database_url = f"sqlite:///{database_path}"
    _alembic(database_url, "upgrade", "20260804_0001")
    _alembic(database_url, "upgrade", "head")

    expected = {
        "requested_model",
        "actual_model",
        "response_id",
        "request_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "transport_retry_count",
    }
    with sqlite3.connect(database_path) as connection:
        for table_name in ("agent_traces", "scoring_runs"):
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            columns = {row[1]: row for row in rows}
            assert expected <= columns.keys()
            retry = columns["transport_retry_count"]
            assert retry[3] == 1
            assert retry[4] in {"0", "'0'"}

    _alembic(database_url, "downgrade", "20260804_0001")
    with sqlite3.connect(database_path) as connection:
        for table_name in ("agent_traces", "scoring_runs"):
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            assert expected.isdisjoint(columns)


def test_final_scorer_contract_sha_migration_preserves_historical_unknowns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "contract-sha-upgrade.db"
    database_url = f"sqlite:///{database_path}"
    _alembic(database_url, "upgrade", PROVENANCE_REVISION)
    _alembic(database_url, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(scoring_runs)")
        }
        contract_sha = columns["final_scorer_contract_sha256"]
        assert contract_sha[2].upper() == "VARCHAR(64)"
        assert contract_sha[3] == 0
        assert contract_sha[4] is None

    _alembic(database_url, "downgrade", PROVENANCE_REVISION)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(scoring_runs)")
        }
        assert "final_scorer_contract_sha256" not in columns
