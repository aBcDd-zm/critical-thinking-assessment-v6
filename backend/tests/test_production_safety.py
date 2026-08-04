from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app.api.router import database_health
from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_ADMIN_PASSWORD_HASH, TEST_ADMIN_JWT_SECRET, test_engine


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "model_gateway_mode": "real",
        "deepseek_api_key": "deepseek-test-key",
        "admin_username": "admin",
        "admin_password_hash": TEST_ADMIN_PASSWORD_HASH,
        "admin_jwt_secret": TEST_ADMIN_JWT_SECRET,
        "tts_mode": "disabled",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("override", "expected_message"),
    [
        ({"model_gateway_mode": "mock"}, "MODEL_GATEWAY_MODE must be real"),
        ({"deepseek_api_key": "   "}, "DEEPSEEK_API_KEY must be non-empty"),
        ({"admin_username": ""}, "ADMIN_USERNAME must be non-empty"),
        ({"admin_password_hash": ""}, "ADMIN_PASSWORD_HASH must be an Argon2id hash"),
        ({"admin_jwt_secret": "too-short"}, "ADMIN_JWT_SECRET must be at least 32 characters"),
        ({"tts_mode": "fake"}, "TTS_MODE must not be fake"),
    ],
)
def test_production_configuration_fails_closed(
    override: dict[str, object],
    expected_message: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_message):
        _production_settings(**override)


def test_development_configuration_keeps_mock_and_fake_compatibility() -> None:
    config = Settings(
        _env_file=None,
        app_env="development",
        model_gateway_mode="mock",
        deepseek_api_key="",
        admin_username="",
        admin_password_hash="",
        admin_jwt_secret="",
        tts_mode="fake",
    )

    assert config.model_gateway_mode == "mock"
    assert config.tts_mode == "fake"


def test_production_disables_interactive_api_documentation() -> None:
    production_app = create_app(_production_settings())

    assert production_app.docs_url is None
    assert production_app.redoc_url is None
    assert production_app.openapi_url is None
    with TestClient(production_app) as production_client:
        assert production_client.get("/docs").status_code == 404
        assert production_client.get("/redoc").status_code == 404
        assert production_client.get("/openapi.json").status_code == 404


def test_database_health_returns_503_even_if_rollback_also_fails() -> None:
    class BrokenDatabase:
        def execute(self, _statement):
            raise ConnectionError("database is unreachable")

        def rollback(self) -> None:
            raise ConnectionError("rollback is unreachable")

    with pytest.raises(HTTPException) as error:
        database_health(BrokenDatabase())  # type: ignore[arg-type]

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "database_unavailable"


def test_database_health_checks_connectivity_and_alembic_version(client) -> None:
    missing_version = client.get("/api/v1/health/db")
    assert missing_version.status_code == 503
    assert missing_version.json()["detail"]["code"] == "database_unavailable"

    with test_engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260804_0001')"))
    try:
        healthy = client.get("/api/v1/health/db")
        assert healthy.status_code == 200
        assert healthy.json() == {"status": "ok", "alembic_version": "20260804_0001"}
    finally:
        with test_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
