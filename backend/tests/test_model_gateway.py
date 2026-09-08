from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from app.core.config import settings
from app.schemas import (
    AttributedFinalScorerOutput,
    FinalScorerOutput,
    NaturalInterviewerOutput,
)
from app.services.model_gateway import (
    INCREMENTAL_EVIDENCE_PROMPT_ID,
    INCREMENTAL_EVIDENCE_PROMPT_VERSION,
    INCREMENTAL_EVIDENCE_SYSTEM_PROMPT,
    INCREMENTAL_EVIDENCE_SYSTEM_PROMPT_V6_2_0,
    ModelGatewayError,
    ModelGatewayService,
    _interviewer_repetition_keys,
    _latest_user_explicitly_switches_decision,
    _latest_user_requests_interview_end,
    _latest_user_requests_question_repetition,
    _normalize_interviewer_message_for_repetition,
    _validate_v621_opening_contract,
    build_interview_anchor_candidates,
    source_clarification_required,
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


def test_v621_opening_multiple_questions_are_repaired_inside_the_typed_call(
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
        return {
            "interviewer_message": (
                "最近有没有一件让你认真判断或权衡的具体事情吗？"
                "当时最难判断的是什么？"
                if len(calls) == 1
                else "最近哪一件具体事情最需要你认真判断或权衡？"
            ),
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
    assert result.output.interviewer_message == "最近哪一件具体事情最需要你认真判断或权衡？"
    assert "opening_must_have_single_primary_question" in calls[1][-1]["content"]


@pytest.mark.parametrize(
    "message",
    [
        "你好。",
        "？",
        "请先讲一件具体事情﹖再说当时最难判断的是什么﹖",
        "请先讲一件具体事情؟再说当时最难判断的是什么؟",
    ],
)
def test_v621_opening_requires_one_substantive_unicode_question(message: str) -> None:
    with pytest.raises(ValueError, match="opening_must_have_single_primary_question"):
        _validate_v621_opening_contract(
            NaturalInterviewerOutput(
                interviewer_message=message,
                session_action="continue",
                finish_reason=None,
                navigation=None,
            )
        )


@pytest.mark.parametrize(
    "dimension_label",
    [
        "problem_definition",
        "evidence_evaluation",
        "reasoning_argumentation",
        "multiple_perspectives",
        "integrative_decision",
        "dynamic_adjustment",
    ],
)
def test_v621_navigation_neutralizes_dimension_labels(
    dimension_label: str,
) -> None:
    output = NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": "这项依据会怎样改变你最后的选择？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": {
                    "turn_index": 1,
                    "quote": "我会先核实来源。",
                    "start": 0,
                    "end": 8,
                    "text_hash": None,
                },
                "focus_kind": dimension_label,
                "mainline_relation": "core",
            },
        }
    )

    assert output.navigation is not None
    assert output.navigation.focus_kind == "other"


def test_v621_navigation_still_rejects_unknown_focus_kind() -> None:
    with pytest.raises(ValueError, match="focus_kind"):
        NaturalInterviewerOutput.model_validate(
            {
                "interviewer_message": "这项依据会怎样改变你最后的选择？",
                "session_action": "continue",
                "finish_reason": None,
                "navigation": {
                    "decision_anchor": {
                        "turn_index": 1,
                        "quote": "我会先核实来源。",
                        "start": 0,
                        "end": 8,
                        "text_hash": None,
                    },
                    "focus_kind": "evidence_quality",
                    "mainline_relation": "core",
                },
            }
        )


def test_v621_repeated_prior_question_is_repaired_inside_the_typed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    transcript = [
        {
            "turn_index": 0,
            "role": "assistant",
            "content": "你提到会检查试点是否排除了不活跃学生，当时是什么让你觉得需要专门检查这一点？",
        },
        {
            "turn_index": 1,
            "role": "user",
            "content": "因为试点数据里可能混入了长期未登录的学生。",
        },
        {
            "turn_index": 2,
            "role": "assistant",
            "content": "你后来具体怎么核实这个担心？",
        },
        {
            "turn_index": 3,
            "role": "user",
            "content": "我查了登录时间和任务完成记录。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {
        **payload["anchor_candidates"][-1],
        "text_hash": None,
    }
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                # Surface punctuation and whitespace differ, but this repeats
                # turn 0 behind a natural acknowledgement rather than
                # responding to the participant's new answer.
                "interviewer_message": (
                    "谢谢说明。  你提到会检查试点是否排除了不活跃学生 , "
                    "当时是什么让你觉得需要专门检查这一点?  "
                ),
                "session_action": "continue",
                "finish_reason": None,
                "navigation": {
                    "decision_anchor": selected_anchor,
                    "focus_kind": "basis",
                    "mainline_relation": "core",
                },
            }
        return {
            "interviewer_message": "这些记录最后如何改变了你的判断？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "adjustment",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert result.output.interviewer_message == "这些记录最后如何改变了你的判断？"
    assert "v621_interviewer_repeats_prior_question" in calls[1][-1]["content"]


def test_v621_repeated_question_gets_one_extra_bounded_continuity_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    repeated_question = "你后来具体怎么核实这个担心？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": repeated_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "我查了登录时间和任务完成记录。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": (
                repeated_question if len(calls) <= 2 else "这些记录如何改变了你的判断？"
            ),
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "basis",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 3
    assert result.repair_used is True
    assert result.output.interviewer_message == "这些记录如何改变了你的判断？"
    assert "v621_interviewer_repeats_prior_question" in calls[1][-1]["content"]
    assert "v621_interviewer_repeats_prior_question" in calls[2][-1]["content"]


def test_v621_repeat_exhaustion_uses_an_audited_non_repeating_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    repeated_question = (
        "你提到会分别记录各方的收益与风险再决定试点边界——"
        "那这个试点边界最终会怎么定下来？"
    )
    provider_variant = repeated_question.replace("各方的收益", "各方收益")
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": repeated_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "我会先做小范围试点，再根据投诉和响应时间调整。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def always_repeat(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": provider_variant,
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "tradeoff",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", always_repeat)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert len(calls) == 3
    assert result.attempt_count == 3
    assert result.repair_used is True
    assert result.fallback_used is True
    assert not _interviewer_repetition_keys(
        result.output.interviewer_message
    ).intersection(_interviewer_repetition_keys(repeated_question))
    assert result.output.navigation is not None
    assert result.output.navigation.decision_anchor.quote == payload["anchor_candidates"][-1]["quote"]


def test_v621_anchor_copy_error_gets_one_extra_bounded_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "当时最难判断的是什么？"},
        {
            "turn_index": 1,
            "role": "user",
            "content": "我需要先核实不同来源，再决定是否扩大试点。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    malformed_offset_anchor = {
        **selected_anchor,
        "start": selected_anchor["start"] + 1,
    }
    malformed_hash_anchor = {
        **selected_anchor,
        "text_hash": "abc",
    }
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": "核实这些来源后，哪项结果会改变你的选择？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": (
                    malformed_offset_anchor
                    if len(calls) == 1
                    else (malformed_hash_anchor if len(calls) == 2 else selected_anchor)
                ),
                "focus_kind": "basis",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 3
    assert result.repair_used is True
    assert result.output.navigation is not None
    assert result.output.navigation.decision_anchor.start == selected_anchor["start"]
    assert "decision anchor offsets must exactly bound quote" in calls[1][-1]["content"]
    assert "navigation.decision_anchor.text_hash" in calls[2][-1]["content"]


def test_v621_repeat_normalization_is_invisible_but_preserves_numeric_meaning() -> None:
    assert _normalize_interviewer_message_for_repetition(
        "你为什么\u200b这样判断 ？"
    ) == _normalize_interviewer_message_for_repetition("你为什么这样判断?")
    assert _normalize_interviewer_message_for_repetition(
        "阈值是 5-10 吗？"
    ) != _normalize_interviewer_message_for_repetition("阈值是 510 吗？")
    assert _normalize_interviewer_message_for_repetition(
        "投诉率是 3.5% 吗？"
    ) != _normalize_interviewer_message_for_repetition("投诉率是 35% 吗？")


def test_v621_repeat_keys_compare_the_primary_question_after_acknowledgements() -> None:
    original = "你提到会检查试点样本，当时为什么需要专门检查这一点？"
    with_changed_acknowledgement = "谢谢你的补充。当时为什么需要专门检查这一点？"
    assert _interviewer_repetition_keys(original).intersection(
        _interviewer_repetition_keys(with_changed_acknowledgement)
    )
    assert _interviewer_repetition_keys("谢谢你的这些说明。你后来怎么核实？").intersection(
        _interviewer_repetition_keys("你后来怎么核实？")
    )


def test_v621_repeat_keys_ignore_internal_structural_de_in_long_question() -> None:
    original = (
        "你提到会分别记录各方的收益与风险再决定试点边界——"
        "那这个试点边界最终会怎么定下来？"
    )
    repeated = (
        "你提到会分别记录各方收益与风险再决定试点边界——"
        "那这个试点边界最终会怎么定下来？"
    )
    assert _interviewer_repetition_keys(original).intersection(
        _interviewer_repetition_keys(repeated)
    )
    assert not _interviewer_repetition_keys(
        "试点阈值是 5-10 人吗？"
    ).intersection(_interviewer_repetition_keys("试点阈值是 510 人吗？"))
    assert not _interviewer_repetition_keys(
        "你会怎么定试点边界？"
    ).intersection(_interviewer_repetition_keys("你会怎么验证试点结果？"))


@pytest.mark.parametrize(
    "request_text",
    [
        "你能再说一遍刚才的问题吗？",
        "能再说一遍刚才的问题吗？",
        "抱歉，我能请你把刚才的问题再说一遍吗？",
        "你能把刚才的问题再说一遍吗？",
        "能把刚才的问题再说一遍吗？",
        "可以把刚才的问题再说一遍吗？",
    ],
)
def test_v621_common_explicit_repeat_request_forms_are_recognized(
    request_text: str,
) -> None:
    assert _latest_user_requests_question_repetition(
        {"transcript": [{"role": "user", "content": request_text}]}
    )


@pytest.mark.parametrize(
    "not_a_request",
    [
        "当时客户说：你能把刚才的问题再说一遍吗？然后我重新解释。",
        "我记录了客户的问题：可以把刚才的问题再说一遍吗？之后继续。",
        "请不要再说一遍。",
        "麻烦别再问刚才的问题。",
        "请再说一遍我的回答。",
    ],
)
def test_v621_repeat_intent_rejects_quotes_negation_and_the_wrong_object(
    not_a_request: str,
) -> None:
    assert not _latest_user_requests_question_repetition(
        {"transcript": [{"role": "user", "content": not_a_request}]}
    )


@pytest.mark.parametrize(
    "content",
    [
        "我不想结束访谈，我还想继续。",
        "现在不要生成报告。",
        "我还不应该结束访谈。",
        "先别结束对话。",
        "暂时别生成报告。",
        "我拒绝结束访谈。",
        "我当时决定生成报告，把风险同步给管理层。",
        "我选择结束对话，因为继续争论没有意义。",
        "客户告诉我：请结束本次访谈。",
        "AI回答：请生成本次报告。",
        "外部材料写道：请结束本次访谈。",
        "上文内容是：请生成报告。",
        "系统提示：请结束对话。",
        "以下是一段示例：请结束本次访谈。",
        "朋友发来的消息是：请生成报告。",
    ],
)
def test_v621_end_intent_rejects_explicit_negation(content: str) -> None:
    assert not _latest_user_requests_interview_end(
        {"transcript": [{"role": "user", "content": content}]}
    )


@pytest.mark.parametrize(
    "content",
    [
        "我不想换个话题，继续谈这个决定。",
        "先别换一个问题。",
        "我还不应该换个话题。",
        "客户想换个话题但我不想换。",
        "客户说：请换一个话题。",
        "我引用AI的话：请换一个问题。",
        "题目要求：请换一个问题。",
        "我引用如下：请换一个话题。",
    ],
)
def test_v621_switch_intent_rejects_explicit_negation(content: str) -> None:
    assert not _latest_user_explicitly_switches_decision(
        {"transcript": [{"role": "user", "content": content}]}
    )


@pytest.mark.parametrize(
    "content",
    [
        "我不想继续回答了。",
        "我不想继续访谈了。",
        "我不愿继续回答。",
        "我不愿继续访谈。",
        "我不想继续，请结束本次访谈并生成报告。",
        "所以请结束访谈。",
        "请结束本次访谈，谢谢。",
        "我不想继续回答了，谢谢。",
    ],
)
def test_v621_end_intent_accepts_direct_current_request(content: str) -> None:
    assert _latest_user_requests_interview_end(
        {"transcript": [{"role": "user", "content": content}]}
    )


@pytest.mark.parametrize(
    "content",
    [
        "我们换一个话题吧。",
        "请换个问题。",
        "我想谈另一件事情。",
        "请换一个话题，谢谢。",
    ],
)
def test_v621_switch_intent_accepts_direct_current_request(content: str) -> None:
    assert _latest_user_explicitly_switches_decision(
        {"transcript": [{"role": "user", "content": content}]}
    )


def test_v621_explicit_repeat_request_allows_the_same_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    repeated_question = "你后来具体怎么核实这个担心？"
    transcript = [
        {
            "turn_index": 0,
            "role": "assistant",
            "content": repeated_question,
        },
        {
            "turn_index": 1,
            "role": "user",
            "content": "抱歉我刚才没听清，请把刚才的问题再说一遍。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {
        **payload["anchor_candidates"][-1],
        "text_hash": None,
    }

    def respond(
        _self: ModelGatewayService,
        _messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "interviewer_message": repeated_question,
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "basis",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 1
    assert result.repair_used is False
    assert result.output.interviewer_message == repeated_question


def test_v621_explicit_repeat_request_can_replay_latest_source_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    source_question = "请区分哪些来自外部材料、哪些是你自己的判断，并说明采纳理由？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": source_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "抱歉没听清，请把刚才的问题再说一遍。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}

    def respond(
        _self: ModelGatewayService,
        _messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "interviewer_message": source_question,
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "source_ownership",
                "mainline_relation": "source_clarification",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert payload["source_clarification_required"] is False
    assert result.attempt_count == 1
    assert result.repair_used is False
    assert result.output.interviewer_message == source_question


def test_v621_repeat_request_does_not_allow_an_older_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    old_question = "你为什么担心样本有偏差？"
    latest_question = "你后来具体怎么核实这个担心？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": old_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "因为试点数据可能混入长期未登录的学生。",
        },
        {"turn_index": 2, "role": "assistant", "content": latest_question},
        {
            "turn_index": 3,
            "role": "user",
            "content": "抱歉我没听清，请把刚才的问题再说一遍。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": old_question if len(calls) == 1 else latest_question,
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "basis",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert result.output.interviewer_message == latest_question
    assert "v621_interviewer_repeats_prior_question" in calls[1][-1]["content"]


@pytest.mark.parametrize(
    "narrative",
    [
        "当时对方说没听清，我重新解释了方案，然后继续核实记录。",
        "当时同事问我能重复之前的说明吗，我就重新讲了方案。",
        "客户问我可以再说一遍数据来源吗，之后我们核实了记录。",
    ],
)
def test_v621_narrative_about_someone_not_hearing_does_not_disable_repeat_gate(
    monkeypatch: pytest.MonkeyPatch,
    narrative: str,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    repeated_question = "你后来具体怎么核实这个担心？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": repeated_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": narrative,
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return {
            "interviewer_message": (
                repeated_question if len(calls) == 1 else "这些记录如何改变了你的判断？"
            ),
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "basis",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert "v621_interviewer_repeats_prior_question" in calls[1][-1]["content"]


def test_v621_mock_interviewer_obeys_the_same_no_repeat_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "mock")
    transcript: list[dict[str, object]] = [
        {
            "turn_index": 0,
            "role": "assistant",
            "content": "这里没有标准答案，请说说当时最难判断的是什么？",
        },
        {
            "turn_index": 1,
            "role": "user",
            "content": "我需要核实项目安排、长期计划和现实限制。",
        },
    ]
    seen_questions: set[str] = set()
    gateway = ModelGatewayService()

    for answer_number in range(1, 9):
        payload = {
            "participant": {},
            "transcript": transcript,
            "anchor_candidates": build_interview_anchor_candidates(transcript),
            "source_clarification_required": source_clarification_required(transcript),
        }
        result = gateway.generate_interviewer(payload, prompt_version="v6.2.1")
        question = result.output.interviewer_message

        assert question not in seen_questions
        seen_questions.add(question)
        if answer_number == 8:
            break
        transcript.extend(
            [
                {
                    "turn_index": len(transcript),
                    "role": "assistant",
                    "content": question,
                },
                {
                    "turn_index": len(transcript) + 1,
                    "role": "user",
                    "content": f"这是第{answer_number + 1}次补充，我会继续核实现实条件。",
                },
            ]
        )


@pytest.mark.parametrize(
    "content",
    [
        "我不想结束访谈，我还想继续补充。",
        "现在不要生成报告，我还要说明限制。",
        "我当时决定生成报告，把风险同步给管理层。",
    ],
)
def test_v621_mock_uses_the_same_conservative_end_intent(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "mock")
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "当时最难判断的是什么？"},
        {"turn_index": 1, "role": "user", "content": content},
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }

    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.output.session_action == "continue"
    assert result.output.finish_reason is None


def test_v621_mock_returns_to_mainline_after_one_unresolved_source_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "mock")
    first_clarification = (
        "先把来源分清：其中哪些是外部材料，哪些是你自己的判断与采纳理由？"
    )
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "当时最难判断的是什么？"},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI说应该直接上线，我自己的判断是先试点。",
        },
        {"turn_index": 2, "role": "assistant", "content": first_clarification},
        {
            "turn_index": 3,
            "role": "user",
            "content": "AI和我的说法还是混在一起，我还分不清哪些是外部材料、哪些是我自己的判断。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }

    assert payload["source_clarification_required"] is False
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.output.interviewer_message != first_clarification
    assert "外部" not in result.output.interviewer_message
    assert "自己的判断" not in result.output.interviewer_message
    assert result.output.navigation is not None
    assert result.output.navigation.focus_kind == "decision_problem"
    assert result.output.navigation.mainline_relation == "return"


def test_v621_mock_handles_a_second_independent_mixed_source_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "mock")
    first_clarification = (
        "先把来源分清：其中哪些是外部材料，哪些是你自己的判断与采纳理由？"
    )
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": first_clarification},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI的话是外部材料，先试点是我自己的判断。",
        },
        {
            "turn_index": 2,
            "role": "assistant",
            "content": "回到你的决定，接下来最重要的行动是什么？",
        },
        {
            "turn_index": 3,
            "role": "user",
            "content": "论文说应该直接扩大，但我觉得仍要先小范围核实。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }

    assert payload["source_clarification_required"] is True
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.output.interviewer_message != first_clarification
    assert "哪些内容来自外部材料" in result.output.interviewer_message
    assert "哪些是你自己的判断" in result.output.interviewer_message
    assert result.output.navigation is not None
    assert result.output.navigation.mainline_relation == "source_clarification"


def test_v621_source_return_repair_combines_repeat_and_navigation_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    source_question = "请区分哪些来自外部材料、哪些是你自己的判断，并说明采纳理由？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": source_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI和我的说法仍混在一起，我还无法区分。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                "interviewer_message": source_question,
                "session_action": "continue",
                "finish_reason": None,
                "navigation": {
                    "decision_anchor": selected_anchor,
                    "focus_kind": "source_ownership",
                    "mainline_relation": "source_clarification",
                },
            }
        return {
            "interviewer_message": "先把来源标为不确定后，你现在会采取什么行动？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "action",
                "mainline_relation": "return",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert payload["source_clarification_required"] is False
    assert result.attempt_count == 2
    assert result.repair_used is True
    assert result.output.navigation is not None
    assert result.output.navigation.mainline_relation == "return"
    repair_instruction = calls[1][-1]["content"]
    assert "v621_source_clarification_must_return_to_mainline" in repair_instruction
    assert "不要重复或改写来源归属问题" in repair_instruction


def test_v621_source_return_missing_navigation_gets_the_combined_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    source_question = "请区分哪些来自外部材料、哪些是你自己的判断，并说明采纳理由？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": source_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI和我的说法仍混在一起，我还无法区分。",
        },
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                "interviewer_message": "你接下来会采取什么行动？",
                "session_action": "continue",
                "finish_reason": None,
                "navigation": None,
            }
        return {
            "interviewer_message": "你接下来会采取什么行动？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "action",
                "mainline_relation": "return",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.repair_used is True
    assert "v621_source_clarification_must_return_to_mainline" in (
        calls[1][-1]["content"]
    )


def test_v621_user_can_finish_immediately_after_source_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    source_question = "请区分哪些来自外部材料、哪些是你自己的判断，并说明采纳理由？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": source_question},
        {"turn_index": 1, "role": "user", "content": "我不想继续，请结束访谈。"},
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}

    def respond(
        _self: ModelGatewayService,
        _messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "interviewer_message": "好，谢谢你把这些想法说出来，我们就停在这里。",
            "session_action": "finish",
            "finish_reason": "user_requested",
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "other",
                "mainline_relation": "core",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 1
    assert result.output.session_action == "finish"
    assert result.output.finish_reason == "user_requested"


def test_v621_model_cannot_invent_user_requested_finish_after_source_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_mode", "real")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    source_question = "请区分哪些来自外部材料、哪些是你自己的判断，并说明采纳理由？"
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": source_question},
        {"turn_index": 1, "role": "user", "content": "我还想继续，先把来源标为不确定。"},
    ]
    payload = {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }
    selected_anchor = {**payload["anchor_candidates"][-1], "text_hash": None}
    calls: list[list[dict[str, str]]] = []

    def respond(
        _self: ModelGatewayService,
        messages: list[dict[str, str]],
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if len(calls) == 1:
            return {
                "interviewer_message": "好，我们就停在这里。",
                "session_action": "finish",
                "finish_reason": "user_requested",
                "navigation": {
                    "decision_anchor": selected_anchor,
                    "focus_kind": "other",
                    "mainline_relation": "core",
                },
            }
        return {
            "interviewer_message": "先把来源标为不确定后，你接下来会采取什么行动？",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": "action",
                "mainline_relation": "return",
            },
        }

    monkeypatch.setattr(ModelGatewayService, "_post_json", respond)
    result = ModelGatewayService().generate_interviewer(
        payload,
        prompt_version="v6.2.1",
    )

    assert result.attempt_count == 2
    assert result.output.session_action == "continue"
    assert "v621_user_requested_finish_without_user_intent" in calls[1][-1]["content"]


@pytest.mark.parametrize(
    "ordinary_question",
    [
        "你用了哪些外部数据来源，这些证据如何改变了你自己的判断？",
        "哪些论文来源支持了你自己的判断？",
        "你查了哪些外部数据来源，其中哪些真正改变了你自己的判断？",
        "你比较了哪些论文来源，又有哪些反例挑战了你自己的判断？",
        "你如何区分哪些外部来源更可靠，并说明你自己的判断依据是什么？",
        "你怎样分辨外部数据的质量，再形成你自己的判断？",
        "请区分外部材料与你自己的判断，哪一方更可靠？",
        "请分清外部观点和你自己的判断，你更相信哪一方？",
    ],
)
def test_v621_ordinary_evidence_question_is_not_a_source_clarification_attempt(
    ordinary_question: str,
) -> None:
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": ordinary_question},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI说应该直接上线，但我自己的判断是先试点，目前两种说法仍混在一起。",
        },
    ]

    assert source_clarification_required(transcript) is True


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
    assert "\u7efc\u5408\u51b3\u7b56必须至少直接呈现实际选择" in compact
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


def test_attributed_scorer_bounds_overlong_evidence_summary_lists() -> None:
    dimensions = [
        {
            "dimension_key": key,
            "score": 3,
            "evidence_refs": [{"attribution_span_id": index + 1}],
            "reason": "存在可核验的参与者推理。",
            "confidence": 0.7,
            "sufficient": True,
        }
        for index, key in enumerate(
            (
                "problem_definition",
                "evidence_evaluation",
                "reasoning_argumentation",
                "multiple_perspectives",
                "integrative_decision",
                "dynamic_adjustment",
            )
        )
    ]
    summaries = [
        {"text": f"摘要{index}", "attribution_span_ids": [index]}
        for index in range(1, 6)
    ]

    output = AttributedFinalScorerOutput.model_validate(
        {
            "dimensions": dimensions,
            "strengths": summaries,
            "priorities": summaries[:3],
        }
    )

    assert [item.text for item in output.strengths] == ["摘要1", "摘要2"]
    assert [item.text for item in output.priorities] == ["摘要1", "摘要2"]


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
