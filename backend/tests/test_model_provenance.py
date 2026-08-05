from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.api import router as api_router
from app.core.config import settings
from app.models import AssessmentSession
from app.schemas import NaturalInterviewerOutput
from app.services.model_gateway import (
    ModelGatewayError,
    ModelGatewayService,
    StructuredCallResult,
)
from tests.conftest import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME, TestSession


class _FakeResponse:
    def __init__(self, body: dict, *, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _model_body(
    *,
    model: str,
    content: dict,
    response_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict:
    return {
        "id": response_id,
        "model": model,
        "choices": [
            {"message": {"content": json.dumps(content, ensure_ascii=False)}}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _create_session(client) -> str:
    response = client.post(
        "/api/v1/sessions",
        json={
            "consent_version": "v6-research-pilot-2026-08",
            "consent_given": True,
            "participant": {"display_name": "测试用户", "identity_type": "student"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session"]["uuid"]


def _admin_login(client) -> None:
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text


def _send(client, session_uuid: str, content: str):
    return client.post(
        f"/api/v1/sessions/{session_uuid}/turns:stream",
        json={
            "content": content,
            "client_turn_id": "provenance-turn-0001",
            "input_mode": "text",
            "answer_duration_ms": 1000,
        },
    )


def test_gateway_records_exact_raw_model_ids_and_usage(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")

    body = _model_body(
        model="deepseek-v4-flash",
        content={
            "interviewer_message": "你愿意从哪里说起？",
            "session_action": "continue",
            "finish_reason": None,
        },
        response_id="response-001",
        prompt_tokens=17,
        completion_tokens=5,
    )

    monkeypatch.setattr(
        "app.services.model_gateway.httpx.post",
        lambda *_args, **_kwargs: _FakeResponse(
            body, headers={"x-request-id": "request-001"}
        ),
    )
    result = gateway._typed_call(
        system_prompt="test",
        payload={"participant": {}, "transcript": []},
        schema=NaturalInterviewerOutput,
    )

    assert result.requested_model == "deepseek-v4-flash"
    assert result.actual_model == "deepseek-v4-flash"
    assert result.model == "deepseek-v4-flash"
    assert result.response_id == "response-001"
    assert result.request_id == "request-001"
    assert result.prompt_tokens == 17
    assert result.completion_tokens == 5
    assert result.total_tokens == 22
    assert result.transport_retry_count == 0


def test_interviewer_and_final_scorer_use_independent_output_budgets(
    monkeypatch,
) -> None:
    gateway = ModelGatewayService()
    gateway.mode = "real"
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "deepseek_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "deepseek_max_tokens", 3000)
    monkeypatch.setattr(settings, "deepseek_final_scorer_max_tokens", 8000)
    monkeypatch.setattr(settings, "deepseek_final_scorer_timeout_seconds", 90.0)

    transcript = [
        {
            "role": "user",
            "content": "我会先界定问题边界，再核对来源和可能的反例。",
            "turn_index": 1,
        }
    ]
    scorer_output = gateway._mock_final_scorer({"transcript": transcript})
    responses = iter(
        [
            _model_body(
                model="deepseek-v4-flash",
                content={
                    "interviewer_message": "你愿意具体说说会怎样核对来源吗？",
                    "session_action": "continue",
                    "finish_reason": None,
                },
                response_id="response-interviewer-budget",
                prompt_tokens=20,
                completion_tokens=8,
            ),
            _model_body(
                model="deepseek-v4-flash",
                content=scorer_output.model_dump(mode="json"),
                response_id="response-scorer-budget",
                prompt_tokens=80,
                completion_tokens=120,
            ),
        ]
    )
    observed_budgets: list[int] = []
    observed_timeouts: list[float] = []
    observed_thinking: list[object] = []

    def fake_post(*_args, **kwargs):
        observed_budgets.append(kwargs["json"]["max_tokens"])
        observed_timeouts.append(kwargs["timeout"])
        observed_thinking.append(kwargs["json"].get("thinking"))
        return _FakeResponse(next(responses))

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)

    gateway.generate_interviewer(
        {
            "participant": {},
            "transcript": transcript,
            "completion_gate": {
                "valid_answer_count": 1,
                "minimum_valid_answers": 40,
                "maximum_user_answers": 45,
                "can_model_finish": False,
            },
        }
    )
    gateway.generate_final_scorer({"transcript": transcript})

    assert observed_budgets == [3000, 8000]
    assert observed_timeouts == [30.0, 90.0]
    assert observed_thinking == [None, {"type": "disabled"}]


def test_final_scorer_repair_keeps_the_larger_output_budget(monkeypatch) -> None:
    gateway = ModelGatewayService()
    gateway.mode = "real"
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "deepseek_final_scorer_max_tokens", 8000)
    monkeypatch.setattr(settings, "deepseek_final_scorer_timeout_seconds", 90.0)

    transcript = [
        {
            "role": "user",
            "content": "我会先界定问题边界，再核对来源和可能的反例。",
            "turn_index": 1,
        }
    ]
    valid_output = gateway._mock_final_scorer({"transcript": transcript})
    responses = iter(
        [
            _model_body(
                model="deepseek-v4-flash",
                content={"dimensions": []},
                response_id="response-scorer-invalid",
                prompt_tokens=80,
                completion_tokens=20,
            ),
            _model_body(
                model="deepseek-v4-flash",
                content=valid_output.model_dump(mode="json"),
                response_id="response-scorer-repaired",
                prompt_tokens=95,
                completion_tokens=130,
            ),
        ]
    )
    observed_budgets: list[int] = []
    observed_timeouts: list[float] = []
    observed_thinking: list[object] = []

    def fake_post(*_args, **kwargs):
        observed_budgets.append(kwargs["json"]["max_tokens"])
        observed_timeouts.append(kwargs["timeout"])
        observed_thinking.append(kwargs["json"].get("thinking"))
        return _FakeResponse(next(responses))

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)

    result = gateway.generate_final_scorer({"transcript": transcript})

    assert result.repair_used is True
    assert observed_budgets == [8000, 8000]
    assert observed_timeouts == [90.0, 90.0]
    assert observed_thinking == [
        {"type": "disabled"},
        {"type": "disabled"},
    ]


def test_model_identity_mismatch_hard_stops_without_json_repair(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    calls = {"count": 0}
    raw_candidate = "PRIVATE_CANDIDATE_TEXT_MUST_NOT_BE_PERSISTED"
    body = _model_body(
        model="deepseek-v3",
        content={
            "interviewer_message": raw_candidate,
            "session_action": "continue",
            "finish_reason": None,
        },
        response_id="response-mismatch",
        prompt_tokens=13,
        completion_tokens=4,
    )

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        return _FakeResponse(body, headers={"x-request-id": "request-mismatch"})

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)
    with pytest.raises(ModelGatewayError, match="model_identity_mismatch") as failure:
        gateway._typed_call(
            system_prompt="test",
            payload={"participant": {}, "transcript": []},
            schema=NaturalInterviewerOutput,
        )

    assert calls["count"] == 1
    assert failure.value.repair_used is False
    assert failure.value.repairable is False
    assert failure.value.requested_model == "deepseek-v4-flash"
    assert failure.value.actual_model == "deepseek-v3"
    assert failure.value.response_id == "response-mismatch"
    assert failure.value.request_id == "request-mismatch"
    assert failure.value.total_tokens == 17
    assert raw_candidate not in str(failure.value)


def test_non_2xx_response_preserves_provider_request_id(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    status_response = httpx.Response(429, request=request)

    class _RateLimitedResponse:
        headers = {"x-request-id": "request-rate-limited"}

        @staticmethod
        def raise_for_status() -> None:
            raise httpx.HTTPStatusError(
                "rate limited", request=request, response=status_response
            )

    monkeypatch.setattr(
        "app.services.model_gateway.httpx.post",
        lambda *_args, **_kwargs: _RateLimitedResponse(),
    )

    with pytest.raises(ModelGatewayError, match="status=429") as failure:
        gateway._post_json([{"role": "user", "content": "synthetic"}])

    assert failure.value.requested_model == "deepseek-v4-flash"
    assert failure.value.request_id == "request-rate-limited"
    assert failure.value.actual_model is None
    assert failure.value.response_id is None
    assert failure.value.transient is True
    assert failure.value.repairable is False


def test_rate_limit_retries_transport_without_json_repair(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr("app.services.model_gateway.time.sleep", lambda _seconds: None)
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    responses = iter(
        [
            httpx.Response(
                429,
                request=request,
                headers={"x-request-id": "request-rate-limited"},
            ),
            _FakeResponse(
                _model_body(
                    model="deepseek-v4-flash",
                    content={
                        "interviewer_message": "我们可以慢慢来，你想先从哪一点说起？",
                        "session_action": "continue",
                        "finish_reason": None,
                    },
                    response_id="response-after-rate-limit",
                    prompt_tokens=18,
                    completion_tokens=6,
                ),
                headers={"x-request-id": "request-after-rate-limit"},
            ),
        ]
    )
    observed_messages: list[list[dict[str, str]]] = []

    def fake_post(*_args, **kwargs):
        observed_messages.append([dict(item) for item in kwargs["json"]["messages"]])
        return next(responses)

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)

    result = gateway._typed_call(
        system_prompt="test",
        payload={"participant": {}, "transcript": []},
        schema=NaturalInterviewerOutput,
    )

    assert len(observed_messages) == 2
    assert observed_messages[1] == observed_messages[0]
    assert result.repair_used is False
    assert result.transport_retry_count == 1
    assert result.request_id == "request-after-rate-limit"


def test_repeated_server_error_stops_without_json_repair(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr("app.services.model_gateway.time.sleep", lambda _seconds: None)
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    responses = iter(
        [
            httpx.Response(
                503,
                request=request,
                headers={"x-request-id": "request-unavailable-1"},
            ),
            httpx.Response(
                503,
                request=request,
                headers={"x-request-id": "request-unavailable-2"},
            ),
        ]
    )
    observed_messages: list[list[dict[str, str]]] = []

    def fake_post(*_args, **kwargs):
        observed_messages.append([dict(item) for item in kwargs["json"]["messages"]])
        return next(responses)

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)

    with pytest.raises(ModelGatewayError, match="model_transport_failed") as failure:
        gateway._typed_call(
            system_prompt="test",
            payload={"participant": {}, "transcript": []},
            schema=NaturalInterviewerOutput,
        )

    assert len(observed_messages) == 2
    assert observed_messages[1] == observed_messages[0]
    assert failure.value.repair_used is False
    assert failure.value.transient is True
    assert failure.value.repairable is False
    assert failure.value.transport_retry_count == 1
    assert failure.value.request_id == "request-unavailable-2"


def test_non_retryable_client_error_stops_without_json_repair(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    observed_messages: list[list[dict[str, str]]] = []

    def fake_post(*_args, **kwargs):
        observed_messages.append([dict(item) for item in kwargs["json"]["messages"]])
        return httpx.Response(
            400,
            request=request,
            headers={"x-request-id": "request-bad-input"},
        )

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)

    with pytest.raises(ModelGatewayError, match="status=400") as failure:
        gateway._typed_call(
            system_prompt="test",
            payload={"participant": {}, "transcript": []},
            schema=NaturalInterviewerOutput,
        )

    assert len(observed_messages) == 1
    assert failure.value.repair_used is False
    assert failure.value.transient is False
    assert failure.value.repairable is False
    assert failure.value.transport_retry_count == 0
    assert failure.value.request_id == "request-bad-input"


def test_json_repair_aggregates_usage_but_keeps_accepted_response_ids(monkeypatch) -> None:
    gateway = ModelGatewayService()
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    responses = iter(
        [
            _FakeResponse(
                _model_body(
                    model="deepseek-v4-flash",
                    content={
                        "interviewer_message": "你愿意从哪里说起？",
                        "session_action": "continue",
                        "finish_reason": None,
                        "forbidden_extra": "reject-me",
                    },
                    response_id="response-invalid",
                    prompt_tokens=11,
                    completion_tokens=2,
                ),
                headers={"x-request-id": "request-invalid"},
            ),
            _FakeResponse(
                _model_body(
                    model="deepseek-v4-flash",
                    content={
                        "interviewer_message": "我们可以慢慢来，你想先从哪一点说起？",
                        "session_action": "continue",
                        "finish_reason": None,
                    },
                    response_id="response-accepted",
                    prompt_tokens=9,
                    completion_tokens=3,
                ),
                headers={"x-request-id": "request-accepted"},
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.model_gateway.httpx.post",
        lambda *_args, **_kwargs: next(responses),
    )

    result = gateway._typed_call(
        system_prompt="test",
        payload={"participant": {}, "transcript": []},
        schema=NaturalInterviewerOutput,
    )

    assert result.repair_used is True
    assert result.response_id == "response-accepted"
    assert result.request_id == "request-accepted"
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 5
    assert result.total_tokens == 25


def test_failed_interviewer_model_identity_provenance_is_admin_auditable(
    client, monkeypatch
) -> None:
    session_uuid = _create_session(client)
    gateway = api_router.sessions.orchestrator.gateway

    def fail_with_identity_audit(_payload):
        raise ModelGatewayError(
            "model_identity_mismatch:requested=deepseek-v4-flash:actual=deepseek-v3",
            repairable=False,
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v3",
            response_id="response-failed",
            request_id="request-failed",
            prompt_tokens=21,
            completion_tokens=6,
            total_tokens=27,
            transport_retry_count=0,
        )

    monkeypatch.setattr(gateway, "generate_interviewer", fail_with_identity_audit)
    failed = _send(client, session_uuid, "我想先理清自己现在最在意的问题。")
    assert failed.status_code == 500

    _admin_login(client)
    detail = client.get(f"/api/v1/admin/sessions/{session_uuid}").json()
    trace = next(
        item
        for item in detail["traces"]
        if item["action"] == "natural_interview_turn_failed"
    )
    assert trace["requested_model"] == "deepseek-v4-flash"
    assert trace["actual_model"] == "deepseek-v3"
    assert trace["response_id"] == "response-failed"
    assert trace["request_id"] == "request-failed"
    assert trace["prompt_tokens"] == 21
    assert trace["completion_tokens"] == 6
    assert trace["total_tokens"] == 27
    assert trace["transport_retry_count"] == 0
    assert "candidate" not in json.dumps(trace, ensure_ascii=False).lower()


def test_completed_scoring_run_provenance_is_admin_auditable(client, monkeypatch) -> None:
    session_uuid = _create_session(client)
    response = _send(
        client,
        session_uuid,
        "我会先界定问题边界，核实证据与来源，再检验假设和反例。"
        "我也会听取不同角度，权衡风险后决定，并根据反馈调整。",
    )
    assert response.status_code == 200, response.text
    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        assert session
        session.user_answer_count = 40
        db.commit()

    gateway = api_router.sessions.orchestrator.gateway
    original = gateway.generate_final_scorer

    def scorer_with_provenance(payload):
        output = original(payload).output
        return StructuredCallResult(
            output=output,
            provider="deepseek",
            model="deepseek-v4-flash",
            repair_used=False,
            latency_ms=321,
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v4-flash",
            response_id="response-score",
            request_id="request-score",
            prompt_tokens=101,
            completion_tokens=44,
            total_tokens=145,
            transport_retry_count=1,
        )

    monkeypatch.setattr(gateway, "generate_final_scorer", scorer_with_provenance)
    finalized = client.post(f"/api/v1/sessions/{session_uuid}/finalize")
    assert finalized.status_code == 200, finalized.text

    _admin_login(client)
    run = client.get(f"/api/v1/admin/sessions/{session_uuid}").json()[
        "scoring_runs"
    ][-1]
    assert run["requested_model"] == "deepseek-v4-flash"
    assert run["actual_model"] == "deepseek-v4-flash"
    assert run["response_id"] == "response-score"
    assert run["request_id"] == "request-score"
    assert run["prompt_tokens"] == 101
    assert run["completion_tokens"] == 44
    assert run["total_tokens"] == 145
    assert run["transport_retry_count"] == 1
