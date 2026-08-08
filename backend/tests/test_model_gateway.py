from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import settings
from app.schemas import FinalScorerOutput, NaturalInterviewerOutput
from app.services.model_gateway import ModelGatewayError, ModelGatewayService


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
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: EmptyModelResponse())

    with pytest.raises(ModelGatewayError) as error:
        ModelGatewayService()._post_json([])

    assert str(error.value) == "model_output_empty"
    assert error.value.transient is True


def test_post_json_sends_explicit_thinking_token_and_timeout_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(*_args: object, **kwargs: object) -> JsonModelResponse:
        captured.update(kwargs)
        return JsonModelResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
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
    assert captured["timeout"] == 15


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
    assert captured["primary_timeout_seconds"] == 15.0
    assert captured["total_timeout_seconds"] == 25.0


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


def test_final_scorer_normalizes_provider_name_alias() -> None:
    dimensions = [
        {
            "name": key,
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
        {"dimensions": dimensions, "strengths": [], "priorities": []}
    )

    assert [item.dimension_key for item in output.dimensions] == [
        item["name"] for item in dimensions
    ]
