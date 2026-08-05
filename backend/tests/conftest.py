from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from argon2 import PasswordHasher
import reportlab

# Tests must never inherit a real database, model mode, or billable TTS key
# from the developer shell/backend/.env.
os.environ["DATABASE_URL"] = "sqlite:////tmp/critical-thinking-v6-pytest.db"
os.environ["AUTO_CREATE_DB"] = "false"
os.environ["MODEL_GATEWAY_MODE"] = "mock"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["REPORT_PDF_FONT_PATH"] = str(
    Path(reportlab.__file__).resolve().parent / "fonts" / "Vera.ttf"
)
os.environ["TTS_MODE"] = "fake"
os.environ["DOUBAO_TTS_API_KEY"] = ""
os.environ["DOUBAO_TTS_RESOURCE_ID"] = "seed-tts-2.0"
os.environ["DOUBAO_TTS_SPEAKER"] = ""
TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "test-admin-password"
TEST_ADMIN_PASSWORD_HASH = PasswordHasher(
    time_cost=1, memory_cost=8_192, parallelism=1
).hash(TEST_ADMIN_PASSWORD)
TEST_ADMIN_JWT_SECRET = "test-admin-jwt-secret-that-is-long-enough-for-hs256"
os.environ["ADMIN_USERNAME"] = TEST_ADMIN_USERNAME
os.environ["ADMIN_PASSWORD_HASH"] = TEST_ADMIN_PASSWORD_HASH
os.environ["ADMIN_JWT_SECRET"] = TEST_ADMIN_JWT_SECRET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, get_db
from app.main import app


TEST_DATABASE_URL = "sqlite:////tmp/critical-thinking-v6-pytest.db"
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False, class_=Session)


def override_db() -> Generator[Session, None, None]:
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_db


@pytest.fixture(autouse=True)
def fresh_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client
