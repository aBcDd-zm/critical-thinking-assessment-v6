from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import jwt
from fastapi.responses import Response
from sqlalchemy import select

from app.api.router import _set_admin_cookies
from app.core.config import settings
from app.models import AssessmentSession, ExpertScore, HumanReview
from tests.conftest import (
    TEST_ADMIN_JWT_SECRET,
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    TestSession,
)


def _create_session(client, *, name: str = "复核用户") -> str:
    response = client.post(
        "/api/v1/sessions",
        json={
            "consent_version": "v6.0.0",
            "consent_given": True,
            "participant": {"display_name": name, "identity_type": "student"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session"]["uuid"]


def _login(client) -> dict[str, str]:
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "user": {"username": TEST_ADMIN_USERNAME, "display_name": TEST_ADMIN_USERNAME}
    }
    cookie_headers = response.headers.get_list("set-cookie")
    session_cookie = next(header for header in cookie_headers if header.startswith("admin_session="))
    csrf_cookie = next(
        header for header in cookie_headers if header.startswith("cta_v6_admin_csrf=")
    )
    assert "HttpOnly" in session_cookie
    assert "Max-Age=28800" in session_cookie
    assert "Path=/api/v1/admin" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "Secure" not in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Max-Age=28800" in csrf_cookie
    assert "Path=/;" in csrf_cookie
    assert "SameSite=strict" in csrf_cookie
    assert "Secure" not in csrf_cookie
    csrf_token = client.cookies.get("cta_v6_admin_csrf")
    assert csrf_token
    return {"X-CSRF-Token": csrf_token}


def test_admin_login_cookie_authentication_csrf_and_logout(client) -> None:
    assert client.get("/api/v1/admin/sessions").status_code == 401
    wrong = client.post(
        "/api/v1/admin/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": "not-the-password"},
    )
    assert wrong.status_code == 401

    csrf_headers = _login(client)
    login_cookies = client.cookies
    assert login_cookies.get("admin_session")
    assert login_cookies.get("cta_v6_admin_csrf")
    assert client.get("/api/v1/admin/auth/me").json()["user"]["username"] == TEST_ADMIN_USERNAME

    session_uuid = _create_session(client)
    no_csrf = client.put(
        f"/api/v1/admin/sessions/{session_uuid}/review",
        json={"status": "in_review"},
    )
    assert no_csrf.status_code == 403
    invalid_csrf = client.put(
        f"/api/v1/admin/sessions/{session_uuid}/review",
        json={"status": "in_review"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert invalid_csrf.status_code == 403
    saved = client.put(
        f"/api/v1/admin/sessions/{session_uuid}/review",
        json={"status": "in_review"},
        headers=csrf_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["reviewer"] == TEST_ADMIN_USERNAME

    missing_csrf_logout = client.post("/api/v1/admin/auth/logout")
    assert missing_csrf_logout.status_code == 403
    logout = client.post("/api/v1/admin/auth/logout", headers=csrf_headers)
    assert logout.status_code == 204
    assert client.get("/api/v1/admin/auth/me").status_code == 401


def test_admin_rejects_tampered_and_expired_jwts(client) -> None:
    assert (
        client.get(
            "/api/v1/admin/sessions",
            headers={"Cookie": "admin_session=not-a-jwt"},
        ).status_code
        == 401
    )
    expired_token = jwt.encode(
        {
            "sub": TEST_ADMIN_USERNAME,
            "display_name": TEST_ADMIN_USERNAME,
            "scope": "v6_admin",
            "iat": datetime.now(timezone.utc) - timedelta(hours=9),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        TEST_ADMIN_JWT_SECRET,
        algorithm="HS256",
    )
    assert (
        client.get(
            "/api/v1/admin/sessions",
            headers={"Cookie": f"admin_session={expired_token}"},
        ).status_code
        == 401
    )


def test_admin_cookie_is_secure_only_in_production(monkeypatch) -> None:
    response = Response()
    monkeypatch.setattr(settings, "app_env", "production")
    _set_admin_cookies(
        response,
        session_token="test-session-token",
        csrf_token="test-csrf-token",
    )
    cookie_headers = [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    ]
    assert len(cookie_headers) == 2
    assert all("Secure" in header for header in cookie_headers)


def test_all_existing_admin_routes_require_authentication(client) -> None:
    requests = (
        ("get", "/api/v1/admin/sessions", {}),
        ("get", "/api/v1/admin/sessions/not-a-real-session", {}),
        ("put", "/api/v1/admin/sessions/not-a-real-session/review", {"json": {"status": "pending"}}),
        ("post", "/api/v1/admin/sessions/not-a-real-session/expert-scores", {"json": {"scores": [{"dimension_key": "problem_definition", "score": 4}]}}),
        ("post", "/api/v1/admin/expert-scores:import", {}),
        ("get", "/api/v1/admin/exports/anonymous", {}),
        ("get", "/api/v1/admin/dashboard/overview", {}),
    )
    for method, path, kwargs in requests:
        assert getattr(client, method)(path, **kwargs).status_code == 401


def test_dashboard_uses_aggregates_without_transcript_or_review_note_leakage(client) -> None:
    completed_uuid = _create_session(client, name="已完成用户")
    in_progress_uuid = _create_session(client, name="进行中用户")
    transcript_secret = "DASHBOARD_MUST_NOT_RETURN_THIS_TRANSCRIPT"
    turn = client.post(
        f"/api/v1/sessions/{completed_uuid}/turns:stream",
        json={
            "content": transcript_secret,
            "client_turn_id": "dashboard-turn-0001",
            "input_mode": "text",
            "answer_duration_ms": 1200,
        },
    )
    assert turn.status_code == 200
    finalization = client.post(f"/api/v1/sessions/{completed_uuid}/finalize")
    assert finalization.status_code == 200

    csrf_headers = _login(client)
    review_note = "DASHBOARD_MUST_NOT_RETURN_THIS_REVIEW_NOTE"
    review = client.put(
        f"/api/v1/admin/sessions/{completed_uuid}/review",
        json={"status": "needs_followup", "notes": review_note},
        headers=csrf_headers,
    )
    assert review.status_code == 200
    scores = client.post(
        f"/api/v1/admin/sessions/{completed_uuid}/expert-scores",
        json={"scores": [{"dimension_key": "problem_definition", "score": 4}]},
        headers=csrf_headers,
    )
    assert scores.status_code == 200
    assert scores.json()["reviewer"] == TEST_ADMIN_USERNAME

    db = TestSession()
    try:
        completed = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == completed_uuid)
        )
        in_progress = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == in_progress_uuid)
        )
        assert completed and in_progress
        assert completed.review and completed.review.status == "needs_followup"
        assert db.scalar(
            select(ExpertScore).where(ExpertScore.session_id == completed.id)
        )
        assert db.scalar(
            select(HumanReview).where(HumanReview.session_id == in_progress.id)
        )
    finally:
        db.close()

    overview_response = client.get("/api/v1/admin/dashboard/overview")
    assert overview_response.status_code == 200
    overview = overview_response.json()
    assert overview["measurement"] == {
        "total_sessions": 2,
        "completed_sessions": 1,
        "active_sessions": 1,
        "completion_rate": 0.5,
        "phase_counts": {
            "interviewing": 1,
            "finalizing": 0,
            "completed": 1,
            "exited": 0,
            "safety_stopped": 0,
        },
    }
    assert overview["review_queue"]["pending"] == 1
    assert overview["review_queue"]["needs_followup"] == 1
    assert overview["review_queue"]["expert_scored_sessions"] == 1
    assert overview["pipeline_health"]["reports_generated"] == 1
    assert len(overview["recent_sessions"]) == 2
    serialized = json.dumps(overview, ensure_ascii=False)
    assert transcript_secret not in serialized
    assert review_note not in serialized
    assert "confidence" not in serialized
