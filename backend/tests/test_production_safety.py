from __future__ import annotations

import os
import subprocess
import sys

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
        ({"tts_mode": "doubao", "doubao_tts_api_key": ""}, "DOUBAO_TTS_API_KEY must be non-empty"),
        ({"tts_mode": "doubao", "doubao_tts_resource_id": ""}, "DOUBAO_TTS_RESOURCE_ID must be non-empty"),
        ({"tts_mode": "doubao", "doubao_tts_speaker": ""}, "DOUBAO_TTS_SPEAKER must be non-empty"),
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


def test_interviewer_prompt_version_is_explicit_and_rejects_unknown_values() -> None:
    default_config = Settings(_env_file=None)
    rollback_config = Settings(
        _env_file=None,
        natural_interviewer_prompt_version="v6.0.3",
    )
    candidate_config = Settings(
        _env_file=None,
        natural_interviewer_prompt_version="v6.1.1",
    )

    assert default_config.natural_interviewer_prompt_version == "v6.2.0"
    assert rollback_config.natural_interviewer_prompt_version == "v6.0.3"
    assert candidate_config.natural_interviewer_prompt_version == "v6.1.1"
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            natural_interviewer_prompt_version="v6.0.2",
        )


def test_v621_is_the_code_default_and_production_allows_staged_shadow_rollout(
    monkeypatch,
) -> None:
    monkeypatch.delenv("NATURAL_INTERVIEWER_PROMPT_VERSION", raising=False)
    monkeypatch.delenv("EVIDENCE_ATTRIBUTION_MODE", raising=False)
    default_config = Settings(_env_file=None)
    assert default_config.natural_interviewer_prompt_version == "v6.2.1"
    assert default_config.evidence_attribution_mode == "shadow"

    with pytest.raises(
        ValidationError,
        match="EVIDENCE_ATTRIBUTION_MODE must be shadow or enforce",
    ):
        _production_settings(
            natural_interviewer_prompt_version="v6.2.1",
            evidence_attribution_mode="disabled",
        )
    shadow_production = _production_settings(
        natural_interviewer_prompt_version="v6.2.1",
        evidence_attribution_mode="shadow",
    )
    enforce_production = _production_settings(
        natural_interviewer_prompt_version="v6.2.1",
        evidence_attribution_mode="enforce",
    )
    assert shadow_production.evidence_attribution_mode == "shadow"
    assert enforce_production.evidence_attribution_mode == "enforce"


def test_minimum_user_turn_guard_defaults_to_eight_and_stays_within_cap() -> None:
    assert Settings(_env_file=None).natural_interview_min_user_turns == 8
    assert Settings(
        _env_file=None,
        natural_interview_min_user_turns=40,
    ).natural_interview_min_user_turns == 40
    with pytest.raises(ValidationError):
        Settings(_env_file=None, natural_interview_min_user_turns=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, natural_interview_min_user_turns=41)


def test_v6_0_3_rollback_selects_the_preserved_prompt_in_a_fresh_process() -> None:
    env = os.environ.copy()
    env.update(
        {
            "MODEL_GATEWAY_MODE": "real",
            "NATURAL_INTERVIEWER_PROMPT_VERSION": "v6.0.3",
            "DEEPSEEK_API_KEY": "not-used-by-this-test",
        }
    )
    script = """
from app.services.model_gateway import (
    ModelGatewayService,
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_0_3,
)

captured = {}
service = ModelGatewayService()
service._typed_call = lambda **kwargs: captured.update(kwargs)
service.generate_interviewer({"participant": {}, "transcript": []})
assert NATURAL_INTERVIEWER_PROMPT_ID == "natural_interviewer_v6.0.3"
assert NATURAL_INTERVIEWER_PROMPT_VERSION == "v6.0.3"
assert captured["system_prompt"] == NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_0_3
print("rollback-ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert completed.stdout.strip() == "rollback-ok"


def test_v6_0_5_default_is_isolated_from_the_final_scorer_in_a_fresh_process() -> None:
    env = os.environ.copy()
    env.update(
        {
            "MODEL_GATEWAY_MODE": "real",
            "NATURAL_INTERVIEWER_PROMPT_VERSION": "v6.0.5",
            "DEEPSEEK_API_KEY": "not-used-by-this-test",
        }
    )
    script = """
from app.services.model_gateway import (
    ModelGatewayService,
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_SYSTEM_PROMPT,
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_0_5,
)

captured = []
service = ModelGatewayService()
service._typed_call = lambda **kwargs: captured.append(kwargs)
service.generate_interviewer({"participant": {}, "transcript": []})
service.generate_final_scorer({"participant": {}, "transcript": []})
assert NATURAL_INTERVIEWER_PROMPT_ID == "natural_interviewer_v6.0.5"
assert NATURAL_INTERVIEWER_PROMPT_VERSION == "v6.0.5"
assert captured[0]["system_prompt"] == NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_0_5
assert NATURAL_FINAL_SCORER_PROMPT_ID == "natural_final_scorer_v6.1.0"
assert captured[1]["system_prompt"] == NATURAL_FINAL_SCORER_SYSTEM_PROMPT
print("candidate-isolated-ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert completed.stdout.strip() == "candidate-isolated-ok"


def test_v6_1_1_candidate_is_selectable_without_changing_the_default() -> None:
    env = os.environ.copy()
    env.update(
        {
            "MODEL_GATEWAY_MODE": "real",
            "NATURAL_INTERVIEWER_PROMPT_VERSION": "v6.1.1",
            "DEEPSEEK_API_KEY": "not-used-by-this-test",
        }
    )
    script = """
from app.services.model_gateway import (
    ModelGatewayService,
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_SYSTEM_PROMPT,
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_1_1,
)

captured = []
service = ModelGatewayService()
service._typed_call = lambda **kwargs: captured.append(kwargs)
service.generate_interviewer({"participant": {}, "transcript": []})
service.generate_final_scorer({"participant": {}, "transcript": []})
assert NATURAL_INTERVIEWER_PROMPT_ID == "natural_interviewer_v6.1.1"
assert NATURAL_INTERVIEWER_PROMPT_VERSION == "v6.1.1"
assert captured[0]["system_prompt"] == NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_1_1
assert NATURAL_FINAL_SCORER_PROMPT_ID == "natural_final_scorer_v6.1.0"
assert captured[1]["system_prompt"] == NATURAL_FINAL_SCORER_SYSTEM_PROMPT
print("v611-candidate-isolated-ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert completed.stdout.strip() == "v611-candidate-isolated-ok"


def test_production_doubao_configuration_is_accepted_without_exposing_the_key() -> None:
    config = _production_settings(
        tts_mode="doubao",
        doubao_tts_api_key="doubao-test-key",
        doubao_tts_resource_id="seed-tts-2.0",
        doubao_tts_speaker="zh_female_cancan_uranus_bigtts",
    )

    assert config.tts_mode == "doubao"
    assert config.doubao_tts_resource_id == "seed-tts-2.0"


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
