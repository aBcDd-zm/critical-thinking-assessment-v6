from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.core.database import Base


BACKEND_ROOT = Path(__file__).resolve().parents[1]
INITIAL_REVISION = BACKEND_ROOT / "migrations" / "versions" / "20260804_0001_v6_initial.py"


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
    assert "20260804_0003 (head)" in current.stdout
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
