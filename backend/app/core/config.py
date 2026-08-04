from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
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
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout_seconds: float = 30.0
    deepseek_max_tokens: int = 3000
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
    admin_token: str = ""
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
        if not self.deepseek_api_key.strip():
            violations.append("DEEPSEEK_API_KEY must be non-empty")
        if not self.admin_token.strip():
            violations.append("ADMIN_TOKEN must be non-empty")
        if self.tts_mode.strip().lower() == "fake":
            violations.append("TTS_MODE must not be fake")
        if violations:
            raise ValueError("invalid production configuration: " + "; ".join(violations))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
