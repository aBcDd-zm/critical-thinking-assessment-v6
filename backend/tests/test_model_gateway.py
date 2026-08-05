from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import settings
from app.schemas import NaturalInterviewerOutput
from app.services.model_gateway import ModelGatewayError, ModelGatewayService


class EmptyModelResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": ""}}]}


def test_empty_model_content_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: EmptyModelResponse())

    with pytest.raises(ModelGatewayError) as error:
        ModelGatewayService()._post_json([])

    assert str(error.value) == "model_output_empty"
    assert error.value.transient is True


def test_empty_model_content_retries_original_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    calls: list[list[dict[str, str]]] = []
    responses = iter(
        [
            ModelGatewayError("model_output_empty", transient=True),
            {"interviewer_message": "我在听。", "session_action": "continue", "finish_reason": None},
        ]
    )

    def fake_post_json(messages: list[dict[str, str]]) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ModelGatewayService, "_post_json", lambda self, messages: fake_post_json(messages))
    result = ModelGatewayService()._typed_call(
        system_prompt="test",
        payload={"transcript": []},
        schema=NaturalInterviewerOutput,
    )

    assert result.output.interviewer_message == "我在听。"
    assert result.repair_used is False
    assert len(calls) == 2
    assert calls[0] == calls[1]
