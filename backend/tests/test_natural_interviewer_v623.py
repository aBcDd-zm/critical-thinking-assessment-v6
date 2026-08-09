from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas import NaturalInterviewerOutput
from app.services.model_gateway import (
    ModelGatewayService,
    build_interview_anchor_candidates,
    resolve_natural_interviewer_prompt,
    source_clarification_required,
)


V621_SHA256 = "a2782644f1701c6eefb057e391a062d94791d08805b8a6dd784a9b359e4f7c83"
V623_SHA256 = "e61110850329702aee18c7fde7528ff0489dcbe36409e0ab75ecdf2d954cbd1e"


def _payload(content: str) -> dict[str, object]:
    transcript = [
        {
            "turn_index": 0,
            "role": "assistant",
            "content": "你愿意从哪件具体的事情说起？",
        },
        {"turn_index": 1, "role": "user", "content": content},
    ]
    return {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(transcript),
    }


def _mock_output(message: str) -> NaturalInterviewerOutput:
    return NaturalInterviewerOutput(
        interviewer_message=message,
        session_action="continue",
        finish_reason=None,
    )


def _mock_finish_output() -> NaturalInterviewerOutput:
    return NaturalInterviewerOutput(
        interviewer_message="好的，谢谢你把这些说出来。",
        session_action="finish",
        finish_reason="user_requested",
    )


def test_v623_adds_only_the_candidate_prompt_and_preserves_v621_bytes() -> None:
    v621_id, v621_version, v621_prompt = resolve_natural_interviewer_prompt("v6.2.1")
    v623_id, v623_version, v623_prompt = resolve_natural_interviewer_prompt("v6.2.3")

    assert (v621_id, v621_version) == ("natural_interviewer_v6.2.1", "v6.2.1")
    assert hashlib.sha256(v621_prompt.encode("utf-8")).hexdigest() == V621_SHA256
    assert (v623_id, v623_version) == ("natural_interviewer_v6.2.3", "v6.2.3")
    assert hashlib.sha256(v623_prompt.encode("utf-8")).hexdigest() == V623_SHA256
    assert v623_prompt.startswith(v621_prompt)
    assert "有其原话依据、温和但不套话的承接" in v623_prompt
    assert "如果你愿意" in v623_prompt
    assert "恰好包含一个明确、开放的问题" in v623_prompt
    assert "不改变 V6.2.1 的决策主线、真实原文锚点" in v623_prompt
    assert "source_clarification_required=true" in v623_prompt


def test_v623_prompt_makes_source_clarification_the_final_single_question_check() -> None:
    _, _, prompt = resolve_natural_interviewer_prompt("v6.2.3")
    example = (
        "这里同时有外部材料和你的判断。"
        "请区分哪些来自外部、哪些是你自己的判断，"
        "并说说你采纳了什么及理由？"
    )

    assert "只有 source_clarification_required=false" in prompt
    assert "最终输出前再自检" in prompt
    assert "source_clarification_required=true 且对方未明确要求结束" in prompt
    assert example in prompt
    assert example.count("？") + example.count("?") == 1
    assert "不得拆成二选一、第二个问题或只有陈述的回应" in prompt


def test_v623_is_explicitly_selectable_while_default_stays_v621(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NATURAL_INTERVIEWER_PROMPT_VERSION", raising=False)
    default = Settings(_env_file=None)
    candidate = Settings(
        _env_file=None,
        natural_interviewer_prompt_version="v6.2.3",
    )

    assert default.natural_interviewer_prompt_version == "v6.2.1"
    assert candidate.natural_interviewer_prompt_version == "v6.2.3"


def test_v623_production_requires_enforced_attribution() -> None:
    base = {
        "app_env": "production",
        "model_gateway_mode": "real",
        "natural_interviewer_prompt_version": "v6.2.3",
        "evidence_observer_enabled": True,
        "deepseek_api_key": "test-key",
        "admin_username": "admin",
        "admin_password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$ZGlnaWVzdA",
        "admin_jwt_secret": "x" * 32,
        "tts_mode": "disabled",
    }
    config = Settings(_env_file=None, **base, evidence_attribution_mode="enforce")
    assert config.natural_interviewer_prompt_version == "v6.2.3"

    with pytest.raises(ValidationError, match="EVIDENCE_ATTRIBUTION_MODE must be enforce"):
        Settings(_env_file=None, **base, evidence_attribution_mode="disabled")


def test_v623_mock_uses_a_grounded_gentle_bridge_for_explicit_distress() -> None:
    gateway = ModelGatewayService()
    gateway.mode = "mock"
    result = gateway.generate_interviewer(
        _payload("我失恋了，现在很难过。"),
        prompt_version="v6.2.3",
    )

    assert "不好受" in result.output.interviewer_message
    assert "如果你愿意" in result.output.interviewer_message
    assert result.output.interviewer_message.count("？") == 1
    assert result.output.navigation is not None


def test_v623_normal_continue_requires_one_explicit_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_output("谢谢你把这些说出来。")),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    with pytest.raises(ValueError, match="v623_normal_continue_requires_one"):
        gateway.generate_interviewer(
            _payload("我比较了两个方案，最后选择了风险更低的那个。"),
            prompt_version="v6.2.3",
        )


@pytest.mark.parametrize(
    "boundary",
    [
        "你理解错了，我说的是另一件事。",
        "我不想回答这个。",
        "我没听懂你的问题。",
        "让我缓一缓。",
    ],
)
def test_v623_explicit_interaction_boundary_may_continue_without_a_question(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_output("好的，我先停在这里。")),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    result = gateway.generate_interviewer(_payload(boundary), prompt_version="v6.2.3")
    assert result.output.interviewer_message == "好的，我先停在这里。"


@pytest.mark.parametrize(
    "finish_request",
    [
        "结束访谈。",
        "请结束本次访谈。",
        "我想停止这次对话。",
        "我不想继续回答了。",
        "停止回答。",
        "请不要再问我了。",
        "本次访谈就到这里吧。",
        "我想生成报告。",
        "请生成本次报告。",
        "退出",
        "結束訪談。",
        "停止回答。",
        "quit",
        "stop",
        "end the interview",
        "I want to end this interview.",
        "Please stop asking me.",
    ],
)
def test_v623_explicit_finish_request_cannot_pass_as_question_free_continue(
    monkeypatch: pytest.MonkeyPatch,
    finish_request: str,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_output("好的，我先停在这里。")),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    with pytest.raises(
        ValueError,
        match="v623_explicit_finish_request_requires_finish_user_requested",
    ):
        gateway.generate_interviewer(_payload(finish_request), prompt_version="v6.2.3")


def test_v623_explicit_finish_request_accepts_finish_user_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_finish_output()),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    result = gateway.generate_interviewer(
        _payload("我希望结束这次访谈。"),
        prompt_version="v6.2.3",
    )
    assert result.output.session_action == "finish"
    assert result.output.finish_reason == "user_requested"


@pytest.mark.parametrize(
    "not_a_request",
    [
        "我可以结束访谈吗？",
        "如果我想结束访谈该怎么办？",
        "如果我要结束访谈，我会明确告诉你。",
        "“结束访谈”",
        "他说让我结束访谈。",
        "我不想结束访谈。",
        "项目结束了。",
        "这件事就到这里了。",
        "我想知道什么时候生成报告。",
        "Should I quit?",
        "If I quit, I will tell you clearly.",
        "\"quit\"",
        "He said quit.",
        "I do not want to quit.",
        "The project stopped.",
        "End the project.",
    ],
)
def test_v623_question_hypothesis_quote_and_event_end_are_not_forced_finish(
    monkeypatch: pytest.MonkeyPatch,
    not_a_request: str,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(
            lambda *_args, **_kwargs: _mock_output(
                "我明白你说的是这件事。当时你主要在权衡什么？"
            )
        ),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    result = gateway.generate_interviewer(_payload(not_a_request), prompt_version="v6.2.3")
    assert result.output.session_action == "continue"
    assert result.output.finish_reason is None


def test_v623_does_not_exempt_a_long_narrative_that_mentions_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_output("我明白了。")),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    with pytest.raises(ValueError, match="v623_normal_continue_requires_one"):
        gateway.generate_interviewer(
            _payload("我不想回答这个问题，所以后来改用数据说服了他。"),
            prompt_version="v6.2.3",
        )


def test_v623_correction_does_not_exempt_required_source_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload("你误会了，我说的是 AI 的建议，但我自己认为还要核实。")
    assert payload["source_clarification_required"] is True
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(
            lambda *_args, **_kwargs: _mock_output(
                "好的，我知道这里同时有外部材料和你的判断。"
            )
        ),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    with pytest.raises(
        ValueError,
        match="v623_source_clarification_requires_one_explicit_question",
    ):
        gateway.generate_interviewer(payload, prompt_version="v6.2.3")


def test_v621_still_allows_its_existing_question_free_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ModelGatewayService,
        "_mock_interviewer",
        staticmethod(lambda *_args, **_kwargs: _mock_output("谢谢你把这些说出来。")),
    )
    gateway = ModelGatewayService()
    gateway.mode = "mock"

    result = gateway.generate_interviewer(
        _payload("我比较了两个方案。"),
        prompt_version="v6.2.1",
    )
    assert result.output.interviewer_message == "谢谢你把这些说出来。"
