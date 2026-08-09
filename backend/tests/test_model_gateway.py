from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from app.core.config import settings
from app.schemas import FinalScorerOutput, NaturalInterviewerOutput
from app.services.model_gateway import (
    INCREMENTAL_EVIDENCE_PROMPT_ID,
    INCREMENTAL_EVIDENCE_PROMPT_VERSION,
    INCREMENTAL_EVIDENCE_SYSTEM_PROMPT,
    INCREMENTAL_EVIDENCE_SYSTEM_PROMPT_V6_2_0,
    ModelGatewayError,
    ModelGatewayService,
)


class EmptyModelResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": ""}}]}


class JsonModelResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": "{}"}}]}


def test_empty_model_content_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_response(*_args: object, **_kwargs: object) -> EmptyModelResponse:
        return EmptyModelResponse()

    monkeypatch.setattr(
        "app.services.model_gateway._async_http_post",
        empty_response,
    )

    with pytest.raises(ModelGatewayError) as error:
        ModelGatewayService()._post_json([])

    assert str(error.value) == "model_output_empty"
    assert error.value.transient is True


def test_post_json_sends_explicit_thinking_token_and_timeout_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_post(*_args: object, **kwargs: object) -> JsonModelResponse:
        captured.update(kwargs)
        return JsonModelResponse()

    monkeypatch.setattr(
        "app.services.model_gateway._async_http_post",
        fake_post,
    )
    ModelGatewayService()._post_json(
        [{"role": "user", "content": "test"}],
        max_tokens=512,
        timeout_seconds=15,
        thinking="disabled",
    )

    request_json = captured["json"]
    assert isinstance(request_json, dict)
    assert request_json["max_tokens"] == 512
    assert request_json["thinking"] == {"type": "disabled"}
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == pytest.approx(5)
    assert timeout.pool == pytest.approx(1)
    assert timeout.write == pytest.approx(5)
    assert timeout.read == pytest.approx(15)


def test_empty_model_content_retries_with_json_protocol_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []
    responses = iter(
        [
            ModelGatewayError("model_output_empty", transient=True),
            {"interviewer_message": "我在听。", "session_action": "continue", "finish_reason": None},
        ]
    )

    def fake_post_json(
        messages: list[dict[str, str]], **_kwargs: object
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        ModelGatewayService,
        "_post_json",
        lambda self, messages, **kwargs: fake_post_json(messages, **kwargs),
    )
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
    )

    assert result.output.interviewer_message == "我在听。"
    assert result.repair_used is False
    assert result.attempt_count == 2
    assert len(calls) == 2
    assert calls[1][:-1] == calls[0]
    assert "不能返回空内容" in calls[1][-1]["content"]


def test_final_scorer_uses_its_separate_completion_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    captured: dict[str, object] = {}

    def fake_typed_call(self: ModelGatewayService, **kwargs: object) -> object:
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(ModelGatewayService, "_typed_call", fake_typed_call)

    with pytest.raises(RuntimeError, match="captured"):
        ModelGatewayService().generate_final_scorer({"transcript": []})

    assert captured["max_tokens"] == settings.deepseek_scoring_max_tokens
    assert captured["thinking"] == "enabled"


def test_interviewer_uses_low_latency_non_thinking_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    captured: dict[str, object] = {}

    def fake_typed_call(self: ModelGatewayService, **kwargs: object) -> object:
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(ModelGatewayService, "_typed_call", fake_typed_call)

    with pytest.raises(RuntimeError, match="captured"):
        ModelGatewayService().generate_interviewer(
            {"participant": {}, "transcript": []}
        )

    assert captured["max_tokens"] == 512
    assert captured["thinking"] == "disabled"
    assert captured["primary_timeout_seconds"] == 30.0
    assert captured["total_timeout_seconds"] == 80.0
    assert captured["retry_strategy"] == "interview_resilient"
    assert captured["max_attempts"] == 3


def test_v621_opening_semantics_are_repaired_inside_the_typed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                "interviewer_message": "谢谢，我们到这里结束。",
                "session_action": "finish",
                "finish_reason": "natural_closure",
                "navigation": None,
            }
        return {
            "interviewer_message": "最近有没有一件让你认真判断或权衡的具体事情？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": None,
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_opening(
        {"display_name": "匿名参与者", "identity_type": "other"},
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert result.output.session_action == "continue"
    assert "opening_must_invite_and_continue" in calls[1][-1]["content"]


def test_incremental_evidence_uses_its_own_bounded_non_thinking_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    captured: dict[str, object] = {}

    def fake_typed_call(self: ModelGatewayService, **kwargs: object) -> object:
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(ModelGatewayService, "_typed_call", fake_typed_call)

    with pytest.raises(RuntimeError, match="captured"):
        ModelGatewayService().generate_incremental_evidence(
            {"previous_snapshot": None, "new_user_turns": []}
        )

    assert captured["max_tokens"] == 2000
    assert captured["thinking"] == "disabled"
    assert captured["primary_timeout_seconds"] == 8.0
    assert captured["total_timeout_seconds"] == 15.0


def test_v6_2_1_incremental_prompt_preserves_old_text_and_requires_direct_behavior() -> None:
    assert INCREMENTAL_EVIDENCE_PROMPT_ID == "natural_incremental_evidence_v6.2.1"
    assert INCREMENTAL_EVIDENCE_PROMPT_VERSION == "v6.2.1"
    assert INCREMENTAL_EVIDENCE_SYSTEM_PROMPT.startswith(
        INCREMENTAL_EVIDENCE_SYSTEM_PROMPT_V6_2_0
    )
    assert hashlib.sha256(
        INCREMENTAL_EVIDENCE_SYSTEM_PROMPT_V6_2_0.encode("utf-8")
    ).hexdigest() == "057a11c05588bfd5d430f11591bd2a691b89b28ea573df8a81ddd0e61e55ba51"
    compact = "".join(INCREMENTAL_EVIDENCE_SYSTEM_PROMPT.split())
    assert "没有展示机会、没有谈到、没有说明行动或没有说明调整" in compact
    assert "不能把这些缺失当成低水平行为并给1分" in compact
    assert "数字分数只能由用户直接表达的具体行为支持" in compact
    assert "不能同时使多个维度sufficient=true" in compact
    assert "综合决策必须至少直接呈现实际选择" in compact
    assert "动态调整必须直接呈现已经如何调整" in compact
    assert "证据缺失永远不等于低能力" in compact


def test_two_empty_model_outputs_raise_distinct_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls = 0

    def always_empty(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ModelGatewayError(
            "model_output_empty",
            transient=True,
            error_code="model_output_empty",
        )

    monkeypatch.setattr(ModelGatewayService, "_post_json", always_empty)

    with pytest.raises(ModelGatewayError) as failure:
        ModelGatewayService()._typed_call(
            system_prompt="test",
            payload={"transcript": []},
            schema=NaturalInterviewerOutput,
            total_timeout_seconds=25,
            primary_timeout_seconds=15,
        )

    assert calls == 2
    assert failure.value.error_code == "model_empty_response"
    assert failure.value.attempt_count == 2
    assert failure.value.transient is True


def test_interview_retry_uses_only_the_remaining_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    clock = iter([0.0, 0.0, 4.0, 8.0])
    monkeypatch.setattr(
        "app.services.model_gateway.time.monotonic",
        lambda: next(clock),
    )
    timeouts: list[float] = []

    def respond(
        _self: ModelGatewayService,
        _messages: list[dict[str, str]],
        **kwargs: object,
    ) -> dict[str, object]:
        timeouts.append(float(kwargs["timeout_seconds"]))
        if len(timeouts) == 1:
            raise ModelGatewayError(
                "model_output_empty",
                transient=True,
                error_code="model_output_empty",
            )
        return {
            "interviewer_message": "当时哪条信息最影响你的判断？",
            "session_action": "continue",
            "finish_reason": None,
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
        total_timeout_seconds=25,
        primary_timeout_seconds=15,
    )

    assert timeouts == [15.0, 21.0]
    assert result.latency_ms == 8_000
    assert result.attempt_count == 2


def test_interview_resilient_retry_recovers_transport_then_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []
    responses: list[object] = [
        ModelGatewayError(
            "model_transport_failed:ReadError:closed",
            transient=True,
            error_code="model_transport_failed",
        ),
        {
            "interviewer_message": "你当时为什么这样判断？",
            "session_action": "continue",
            "finish_reason": None,
            "target_dimension": "problem_definition",
        },
        {
            "interviewer_message": "你当时为什么这样判断？",
            "session_action": "continue",
            "finish_reason": None,
        },
    ]

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        result = responses[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, dict)
        return result

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
        total_timeout_seconds=25,
        primary_timeout_seconds=15,
        retry_strategy="interview_resilient",
        max_attempts=3,
    )

    assert result.attempt_count == 3
    assert result.repair_used is True
    assert calls[1] == calls[0]
    assert "修复后的完整 JSON 对象" in calls[2][-1]["content"]


def test_interview_resilient_retry_recovers_contract_then_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                "interviewer_message": "你愿意多说一点吗？",
                "session_action": "continue",
                "finish_reason": None,
                "coverage": 1,
            }
        if len(calls) == 2:
            raise ModelGatewayError(
                "model_http_retryable:HTTPStatusError:503",
                transient=True,
                error_code="model_http_retryable",
            )
        return {
            "interviewer_message": "哪条依据最影响你当时的判断？",
            "session_action": "continue",
            "finish_reason": None,
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
        total_timeout_seconds=25,
        primary_timeout_seconds=15,
        retry_strategy="interview_resilient",
        max_attempts=3,
    )

    assert result.attempt_count == 3
    assert result.repair_used is True
    assert "修复后的完整 JSON 对象" in calls[1][-1]["content"]
    assert calls[2] == calls[1]


def test_model_gateway_error_from_output_validator_remains_repairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []
    validator_calls = 0

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": "哪条依据最影响你的判断？",
            "session_action": "continue",
            "finish_reason": None,
        }

    def validate(_output: NaturalInterviewerOutput) -> None:
        nonlocal validator_calls
        validator_calls += 1
        if validator_calls == 1:
            # Semantic validators may use a stable gateway code. The fact that
            # the default error is not marked repairable must not bypass the
            # typed output repair loop.
            raise ModelGatewayError("semantic_output_contract_failed")

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
        output_validator=validate,
        retry_strategy="interview_resilient",
        max_attempts=3,
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert validator_calls == 2
    assert "semantic_output_contract_failed" in calls[1][-1]["content"]


def test_interview_resilient_retry_does_not_turn_repeated_transport_into_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls = 0

    def always_transport(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ModelGatewayError(
            "model_transport_failed:ReadTimeout",
            transient=True,
            error_code="model_transport_failed",
        )

    monkeypatch.setattr(ModelGatewayService, "_post_json", always_transport)
    with pytest.raises(ModelGatewayError) as failure:
        ModelGatewayService()._typed_call(
            system_prompt="test",
            payload={"transcript": []},
            schema=NaturalInterviewerOutput,
            total_timeout_seconds=25,
            primary_timeout_seconds=15,
            retry_strategy="interview_resilient",
            max_attempts=3,
        )

    assert calls == 2
    assert failure.value.error_code == "model_connection_interrupted"
    assert failure.value.attempt_count == 2
    assert failure.value.repair_used is False


def test_interview_resilient_retry_reserves_budget_for_different_failure_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    clock = iter([0.0, 0.0, 4.0, 8.0])
    monkeypatch.setattr(
        "app.services.model_gateway.time.monotonic",
        lambda: next(clock),
    )
    timeouts: list[float] = []

    def respond(
        _self: ModelGatewayService,
        _messages: list[dict[str, str]],
        **kwargs: object,
    ) -> dict[str, object]:
        timeouts.append(float(kwargs["timeout_seconds"]))
        if len(timeouts) == 1:
            raise ModelGatewayError(
                "model_transport_failed:ReadError",
                transient=True,
                error_code="model_transport_failed",
            )
        return {
            "interviewer_message": "哪条信息最影响你的判断？",
            "session_action": "continue",
            "finish_reason": None,
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
        total_timeout_seconds=25,
        primary_timeout_seconds=15,
        retry_strategy="interview_resilient",
        max_attempts=3,
    )

    assert timeouts == [15.0, 10.5]
    assert result.latency_ms == 8_000


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_http_statuses_are_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(status_code, request=request, json={"error": "not exposed"})
    async def retryable_response(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(
        "app.services.model_gateway._async_http_post",
        retryable_response,
    )

    with pytest.raises(ModelGatewayError) as failure:
        ModelGatewayService()._post_json([])

    assert failure.value.error_code == "model_http_retryable"
    assert failure.value.transient is True
    assert failure.value.repairable is False


def test_permanent_http_rejection_is_not_sent_to_contract_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(401, request=request, json={"error": "not exposed"})
    calls = 0

    async def reject(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(
        "app.services.model_gateway._async_http_post",
        reject,
    )
    with pytest.raises(ModelGatewayError) as failure:
        ModelGatewayService()._typed_call(
            system_prompt="test",
            payload={"transcript": []},
            schema=NaturalInterviewerOutput,
            retry_strategy="interview_resilient",
            max_attempts=3,
        )

    assert calls == 1
    assert failure.value.error_code == "model_http_rejected"
    assert failure.value.transient is False
    assert failure.value.repair_used is False
    assert failure.value.attempt_count == 1


def test_provider_request_has_a_cancellable_cumulative_wall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = False

    async def never_finishes(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        finally:
            cancelled = True

    monkeypatch.setattr(
        "app.services.model_gateway._async_http_post",
        never_finishes,
    )
    started = time.monotonic()
    with pytest.raises(ModelGatewayError) as failure:
        ModelGatewayService()._post_json([], timeout_seconds=0.02)

    assert time.monotonic() - started < 0.5
    assert cancelled is True
    assert failure.value.error_code == "model_transport_deadline_exceeded"
    assert failure.value.transient is True


def test_final_scorer_bounds_overlong_summary_lists() -> None:
    dimensions = [
        {
            "dimension_key": key,
            "score": None,
            "quotes": [],
            "reason": "证据有限，未充分测得该视角。",
            "confidence": 0.0,
            "sufficient": False,
        }
        for key in (
            "problem_definition",
            "evidence_evaluation",
            "reasoning_argumentation",
            "multiple_perspectives",
            "integrative_decision",
            "dynamic_adjustment",
        )
    ]

    output = FinalScorerOutput.model_validate(
        {
            "dimensions": dimensions,
            "strengths": ["一", "二", "三", "四", "五"],
            "priorities": ["甲", "乙", "丙"],
        }
    )

    assert output.strengths == ["一", "二"]
    assert output.priorities == ["甲", "乙"]


def test_interviewer_normalizes_provider_json_null_string() -> None:
    output = NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": "当时哪条信息最影响你的判断？",
            "session_action": "continue",
            "finish_reason": "null",
        }
    )

    assert output.finish_reason is None


def test_final_scorer_normalizes_dimension_keyed_object() -> None:
    dimension_map = {
        key: {
            "score": None,
            "quotes": [],
            "reason": "证据有限，未充分测得该视角。",
            "confidence": 0.0,
            "sufficient": False,
        }
        for key in (
            "problem_definition",
            "evidence_evaluation",
            "reasoning_argumentation",
            "multiple_perspectives",
            "integrative_decision",
            "dynamic_adjustment",
        )
    }

    output = FinalScorerOutput.model_validate(
        {
            "dimensions": dimension_map,
            "strengths": [],
            "priorities": [],
        }
    )

    assert [item.dimension_key for item in output.dimensions] == list(dimension_map)


@pytest.mark.parametrize("alias", ["name", "dimension", "key"])
def test_final_scorer_normalizes_provider_dimension_alias(alias: str) -> None:
    dimension_keys = (
        "problem_definition",
        "evidence_evaluation",
        "reasoning_argumentation",
        "multiple_perspectives",
        "integrative_decision",
        "dynamic_adjustment",
    )
    dimensions = [
        {
            alias: key,
            "score": None,
            "quotes": [],
            "reason": "证据有限，未充分测得该视角。",
            "confidence": 0.0,
            "sufficient": False,
        }
        for key in dimension_keys
    ]

    output = FinalScorerOutput.model_validate(
        {"dimensions": dimensions, "strengths": [], "priorities": []}
    )

    assert [item.dimension_key for item in output.dimensions] == list(dimension_keys)


def test_final_scorer_normalizes_provider_observation_alias() -> None:
    dimensions = [
        {
            "dimension_key": key,
            "score": None,
            "quotes": [],
            "observation": "证据有限，未充分测得该视角。",
            "confidence": 0.0,
            "sufficient": False,
        }
        for key in (
            "problem_definition",
            "evidence_evaluation",
            "reasoning_argumentation",
            "multiple_perspectives",
            "integrative_decision",
            "dynamic_adjustment",
        )
    ]

    output = FinalScorerOutput.model_validate(
        {"dimensions": dimensions, "strengths": [], "priorities": []}
    )

    assert all(item.reason == "证据有限，未充分测得该视角。" for item in output.dimensions)
