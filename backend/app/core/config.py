from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "思衡 V6 完全自然访谈实验版"
    database_url: str = "sqlite:///./data/v6.db"
    # Schema changes are owned by Alembic.  Keeping this disabled by default
    # prevents application startup from silently drifting away from the
    # versioned migration history.
    auto_create_db: bool = False
    model_gateway_mode: Literal["real", "mock"] = "real"
    # Prompt selection is explicit so a deployment can roll back conversational
    # style without changing code or losing the version recorded in traces.
    natural_interviewer_prompt_version: Literal[
        "v6.0.3", "v6.0.4", "v6.0.5", "v6.1.1", "v6.2.0", "v6.2.1"
    ] = "v6.2.1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    # Final scoring includes the full six-dimension rubric and can take
    # longer than an interviewer turn. Synchronous scoring routes keep a
    # 200-second reverse-proxy budget for at most two 90-second attempts.
    deepseek_timeout_seconds: float = 90.0
    deepseek_max_tokens: int = 3000
    # Interview turns use a deliberately smaller, non-thinking profile. The
    # first request receives at most 15 seconds and a single retry may use the
    # rest of the 25-second wall-clock budget.
    deepseek_interview_thinking: Literal["enabled", "disabled"] = "disabled"
    deepseek_interview_max_tokens: int = 512
    deepseek_interview_primary_timeout_seconds: float = 15.0
    deepseek_interview_total_timeout_seconds: float = 25.0
    # Final scoring spends tokens on six dimensions plus exact evidence quotes;
    # keep its completion budget separate from short interviewer turns.
    deepseek_scoring_max_tokens: int = 12000
    # Evidence snapshots run independently after each saved answer. They must
    # never delay the interviewer stream, and their validated output is reused
    # directly when the participant generates a report.
    evidence_observer_enabled: bool = True
    # Attribution is release-gated independently from the interviewer prompt.
    # ``shadow`` records the new ownership analysis and span-scoring comparison
    # while preserving the existing participant result. ``enforce`` becomes a
    # hard gate only for sessions whose opening bound them to v6.2.1.
    evidence_attribution_mode: Literal["disabled", "shadow", "enforce"] = "shadow"
    deepseek_evidence_thinking: Literal["enabled", "disabled"] = "disabled"
    deepseek_evidence_max_tokens: int = 2000
    deepseek_evidence_primary_timeout_seconds: float = 8.0
    deepseek_evidence_total_timeout_seconds: float = 15.0
    # Active closure is allowed only after this many distinct, persisted user
    # answers. This is a lower guardrail rather than a forced stopping point.
    natural_interview_min_user_turns: int = Field(default=8, ge=1, le=40)
    tts_mode: Literal["fake", "doubao", "disabled"] = "fake"
    doubao_tts_api_key: str = ""
    doubao_tts_resource_id: str = "seed-tts-2.0"
    doubao_tts_speaker: str = "zh_female_cancan_uranus_bigtts"
    doubao_tts_speech_rate: int = -5
    doubao_tts_bit_rate: int = 128_000
    doubao_tts_context_text: str = (
        "请像一位成熟、温和、专注的访谈者面对面自然交谈。语气真诚克制，不要播音腔，"
        "不要教学腔，不要逐字重读；停顿自然，句尾轻收。"
    )
    doubao_tts_timeout_seconds: float = 25.0
    doubao_tts_max_attempts: int = 2
    # The V6 review console has one deliberately small administrator surface.
    # Keep credentials in deployment environment variables; neither the
    # plaintext password nor an API token belongs in the frontend bundle.
    admin_username: str = ""
    admin_password_hash: str = ""
    admin_jwt_secret: str = ""
    cors_origins: list[str] = ["http://127.0.0.1:5176", "http://localhost:5176"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_secrets_and_providers(self) -> "Settings":
        """Fail closed before a production process can start insecurely."""

        if self.app_env != "production":
            return self

        violations: list[str] = []
        if self.model_gateway_mode != "real":
            violations.append("MODEL_GATEWAY_MODE must be real")
        if (
            self.natural_interviewer_prompt_version == "v6.2.1"
            and self.evidence_attribution_mode != "enforce"
        ):
            violations.append(
                "EVIDENCE_ATTRIBUTION_MODE must be enforce when "
                "NATURAL_INTERVIEWER_PROMPT_VERSION is v6.2.1"
            )
        if not self.deepseek_api_key.strip():
            violations.append("DEEPSEEK_API_KEY must be non-empty")
        if not self.admin_username.strip():
            violations.append("ADMIN_USERNAME must be non-empty")
        if not self.admin_password_hash.strip().startswith("$argon2id$"):
            violations.append("ADMIN_PASSWORD_HASH must be an Argon2id hash")
        if len(self.admin_jwt_secret.strip()) < 32:
            violations.append("ADMIN_JWT_SECRET must be at least 32 characters")
        if self.tts_mode.strip().lower() == "fake":
            violations.append("TTS_MODE must not be fake")
        if self.tts_mode == "doubao":
            if not self.doubao_tts_api_key.strip():
                violations.append("DOUBAO_TTS_API_KEY must be non-empty when TTS_MODE is doubao")
            if not self.doubao_tts_resource_id.strip():
                violations.append("DOUBAO_TTS_RESOURCE_ID must be non-empty when TTS_MODE is doubao")
            if not self.doubao_tts_speaker.strip():
                violations.append("DOUBAO_TTS_SPEAKER must be non-empty when TTS_MODE is doubao")
        if violations:
            raise ValueError("invalid production configuration: " + "; ".join(violations))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
