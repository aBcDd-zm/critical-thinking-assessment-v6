from __future__ import annotations

import hashlib
import re

import pytest
from pydantic import ValidationError

from app.domain import final_scorer_contract as frozen_contract
from app.domain.catalog import DIMENSIONS
from app.services.model_gateway import (
    FINAL_SCORER_BARS_PROMPT_CONTRACT,
    ModelGatewayError,
    ModelGatewayService,
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_PROMPT_VERSION,
    NATURAL_FINAL_SCORER_SYSTEM_PROMPT,
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT,
)
from app.schemas import FinalScorerOutput, ScoringDimensionOutput


EXPECTED_BARS_CONTRACT_SHA256 = (
    "7f0cc87f0b3e0a6cae5b8c65e3208d6be2944bff194f6d540060f16290c77984"
)
EXPECTED_INTERVIEWER_PROMPT_SHA256 = (
    "76c59963cdc639cfc7b7a44e30241b126d4814fd7a0ddca55cdafe623a10245a"
)


def test_frozen_final_scorer_contract_has_six_dimensions_and_five_bars_levels() -> None:
    payload = frozen_contract.final_scorer_bars_contract_payload()

    assert payload["contract_version"] == "v6.2.0"
    assert [item["rule_key"] for item in payload["adjudication_rules"]] == [
        key for key, _ in frozen_contract.FINAL_SCORER_ADJUDICATION_RULES
    ]
    assert all(item["rule"].strip() for item in payload["adjudication_rules"])
    assert [item["dimension_key"] for item in payload["dimensions"]] == [
        dimension.key for dimension in DIMENSIONS
    ]
    assert len(payload["dimensions"]) == 6
    for item, dimension in zip(payload["dimensions"], DIMENSIONS, strict=True):
        assert item["dimension_name"] == dimension.name
        assert item["description"] == dimension.description
        assert item["observable_behaviors"] == list(dimension.observable_behaviors)
        assert [bar["level"] for bar in item["bars"]] == [1, 2, 3, 4, 5]
        assert [bar["anchor"] for bar in item["bars"]] == [
            dimension.bars[level] for level in range(1, 6)
        ]
        assert all(bar["anchor"].strip() for bar in item["bars"])


def test_final_scorer_contract_version_hash_and_prompt_are_frozen() -> None:
    assert frozen_contract.FINAL_SCORER_BARS_CONTRACT_VERSION == "v6.2.0"
    assert re.fullmatch(
        r"[0-9a-f]{64}", frozen_contract.FINAL_SCORER_BARS_CONTRACT_SHA256
    )
    assert frozen_contract.FINAL_SCORER_BARS_CONTRACT_SHA256 == (
        EXPECTED_BARS_CONTRACT_SHA256
    )
    assert (
        frozen_contract.calculate_final_scorer_bars_contract_sha256()
        == EXPECTED_BARS_CONTRACT_SHA256
    )
    assert (
        frozen_contract.assert_final_scorer_bars_contract_integrity()
        == EXPECTED_BARS_CONTRACT_SHA256
    )

    assert NATURAL_FINAL_SCORER_PROMPT_ID == "natural_final_scorer_v6.2.2"
    assert NATURAL_FINAL_SCORER_PROMPT_VERSION == "v6.2.2"
    assert "可观察行为与 BARS 1–5" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "合同版本：v6.2.0" in FINAL_SCORER_BARS_PROMPT_CONTRACT
    assert f"合同 SHA256：{EXPECTED_BARS_CONTRACT_SHA256}" in (
        FINAL_SCORER_BARS_PROMPT_CONTRACT
    )
    for dimension in DIMENSIONS:
        for level in range(1, 6):
            assert f"BARS {level}：{dimension.bars[level]}" in (
                NATURAL_FINAL_SCORER_SYSTEM_PROMPT
            )
    assert "最高完全满足等级" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "部分满足高级锚点不得向上取整" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "跨轮证据冲突" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "新近表述不会因更新而自动覆盖" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "未获充分观察机会均必须 score=null" in (
        NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    )
    assert "opportunity_observed" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "每个有分维度只返回 1–2 条最具诊断性" in (
        NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    )
    assert "`reason` 只写一句简洁的裁决理由" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "优先完成整个 JSON 对象" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "不得复制整段逐字稿或复述 BARS 合同" in (
        NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    )
    assert "绝不能据此" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "忽略其他证据、降低分数或改变 BARS 裁决" in (
        NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    )
    assert "影响判级的跨轮冲突" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "决定性限制" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "不输出思考过程" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT


def test_contract_rejects_missing_or_reordered_adjudication_rules() -> None:
    payload = frozen_contract.final_scorer_bars_contract_payload()
    payload["adjudication_rules"] = payload["adjudication_rules"][1:]

    with pytest.raises(
        frozen_contract.FinalScorerContractError,
        match="adjudication_rule_order_or_membership_changed",
    ):
        frozen_contract._validate_contract_shape(payload)


def test_interviewer_prompt_v6_1_1_is_byte_for_byte_frozen() -> None:
    assert NATURAL_INTERVIEWER_PROMPT_ID == "natural_interviewer_v6.1.1"
    assert NATURAL_INTERVIEWER_PROMPT_VERSION == "v6.1.1"
    assert (
        hashlib.sha256(NATURAL_INTERVIEWER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        == EXPECTED_INTERVIEWER_PROMPT_SHA256
    )


def test_no_observation_opportunity_can_never_be_encoded_as_level_one() -> None:
    with pytest.raises(
        ValidationError,
        match="numeric score requires a dimension-specific observation opportunity",
    ):
        ScoringDimensionOutput.model_validate(
            {
                "dimension_key": "problem_definition",
                "score": 1,
                "quotes": [{"turn_index": 1, "quote": "我不知道"}],
                "reason": "没有形成观察机会。",
                "confidence": 0.2,
                "sufficient": True,
                "opportunity_observed": False,
            }
        )

    output = ScoringDimensionOutput.model_validate(
        {
            "dimension_key": "problem_definition",
            "score": None,
            "quotes": [],
            "reason": "本次未触及该维度。",
            "confidence": 0.0,
            "sufficient": False,
            "opportunity_observed": False,
        }
    )
    assert output.score is None


def _compact_final_output_payload() -> dict:
    return {
        "dimensions": [
            {
                "dimension_key": dimension.key,
                "score": 3,
                "quotes": [{"turn_index": 1, "quote": "我会先核对现有记录。"}],
                "reason": "用户原话支持 BARS 3，但未呈现更高等级所需的跨来源核验。",
                "confidence": 0.7,
                "sufficient": True,
                "opportunity_observed": True,
            }
            for dimension in DIMENSIONS
        ],
        "strengths": ["能先核对已有记录，再形成暂时判断。"],
        "priorities": ["本次对话尚未呈现跨来源核验过程。"],
    }


def test_final_scorer_compact_output_limits_reject_instead_of_truncating() -> None:
    assert FinalScorerOutput.model_validate(_compact_final_output_payload())

    too_many_quotes = _compact_final_output_payload()
    too_many_quotes["dimensions"][0]["quotes"] = [
        {"turn_index": index, "quote": f"证据{index}"} for index in range(1, 4)
    ]
    with pytest.raises(ValidationError, match="List should have at most 2 items"):
        FinalScorerOutput.model_validate(too_many_quotes)

    long_quote = _compact_final_output_payload()
    long_quote["dimensions"][0]["quotes"][0]["quote"] = "证" * 161
    with pytest.raises(ValidationError, match="at most 160 characters"):
        FinalScorerOutput.model_validate(long_quote)

    long_reason = _compact_final_output_payload()
    long_reason["dimensions"][0]["reason"] = "理" * 181
    with pytest.raises(ValidationError, match="at most 180 characters"):
        FinalScorerOutput.model_validate(long_reason)

    for field in ("strengths", "priorities"):
        long_summary = _compact_final_output_payload()
        long_summary[field] = ["结" * 121]
        with pytest.raises(ValidationError, match="at most 120 characters"):
            FinalScorerOutput.model_validate(long_summary)


@pytest.mark.parametrize("frozen_digest", ["", "0" * 64])
def test_real_final_scoring_rejects_missing_or_drifted_contract_before_model_call(
    monkeypatch: pytest.MonkeyPatch, frozen_digest: str
) -> None:
    monkeypatch.setattr(
        frozen_contract,
        "FINAL_SCORER_BARS_CONTRACT_SHA256",
        frozen_digest,
    )
    gateway = ModelGatewayService()
    gateway.mode = "real"

    def unexpected_model_call(**_kwargs):  # pragma: no cover - assertion sentinel
        raise AssertionError("real model must not be called with an invalid contract")

    monkeypatch.setattr(gateway, "_typed_call", unexpected_model_call)

    with pytest.raises(ModelGatewayError, match="final_scorer_contract_invalid"):
        gateway.generate_final_scorer({"transcript": []})
