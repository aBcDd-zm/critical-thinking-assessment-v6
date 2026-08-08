from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    AttributedFinalScorerOutput,
    EvidenceAttributionOutput,
    FinalScorerOutput,
    NaturalInterviewerOutput,
)


DIMENSION_KEYS = (
    "problem_definition",
    "evidence_evaluation",
    "reasoning_argumentation",
    "multiple_perspectives",
    "integrative_decision",
    "dynamic_adjustment",
)


def _span(*, start: int, quote: str, owner: str = "participant_owned") -> dict:
    return {
        "turn_index": 1,
        "quote": quote,
        "start": start,
        "end": start + len(quote),
        "text_hash": "a" * 64,
        "owner": owner,
        "relation": "own_reasoning",
        "elicitation_level": "open_probe",
        "source_label": None,
        "confidence": 0.9,
        "reason": "参与者明确说明了自己的取舍。",
    }


def test_attribution_output_accepts_adjacent_spans_and_rejects_overlap() -> None:
    output = EvidenceAttributionOutput.model_validate(
        {"spans": [_span(start=0, quote="自己的判断"), _span(start=5, quote="外部材料")]}
    )
    assert len(output.spans) == 2

    with pytest.raises(ValidationError, match="must not overlap"):
        EvidenceAttributionOutput.model_validate(
            {"spans": [_span(start=0, quote="自己的判断"), _span(start=4, quote="断和材料")]}
        )


def test_attribution_span_rejects_inexact_offsets_and_extra_fields() -> None:
    inexact = _span(start=0, quote="参与者原话")
    inexact["end"] += 1
    with pytest.raises(ValidationError, match="exactly bound quote"):
        EvidenceAttributionOutput.model_validate({"spans": [inexact]})

    extra = _span(start=0, quote="参与者原话")
    extra["eligibility"] = "eligible"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvidenceAttributionOutput.model_validate({"spans": [extra]})


def test_navigation_is_optional_but_strict_when_present() -> None:
    legacy = NaturalInterviewerOutput(
        interviewer_message="请继续说说你的考虑。",
        session_action="continue",
    )
    assert legacy.navigation is None

    attributed = NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": "你最终如何取舍？",
            "session_action": "continue",
            "navigation": {
                "decision_anchor": {
                    "turn_index": 1,
                    "quote": "是否接受方案",
                    "start": 0,
                    "end": 6,
                    "text_hash": "b" * 64,
                },
                "focus_kind": "tradeoff",
                "mainline_relation": "return",
            },
        }
    )
    assert attributed.navigation is not None
    assert attributed.navigation.mainline_relation == "return"


def test_attributed_scorer_uses_only_span_ids_for_scores_and_summaries() -> None:
    dimensions = [
        {
            "dimension_key": key,
            "score": 3,
            "evidence_refs": [{"attribution_span_id": index + 1}],
            "reason": "由已验证归属片段支持。",
            "confidence": 0.8,
            "sufficient": True,
        }
        for index, key in enumerate(DIMENSION_KEYS)
    ]
    output = AttributedFinalScorerOutput.model_validate(
        {
            "dimensions": dimensions,
            "strengths": [
                {"text": "能说明取舍依据。", "attribution_span_ids": [1, 2]}
            ],
            "priorities": [
                {"text": "补充结果核验。", "attribution_span_ids": [6]}
            ],
        }
    )
    assert output.dimensions[0].evidence_refs[0].attribution_span_id == 1
    assert output.strengths[0].attribution_span_ids == [1, 2]

    dimensions[0]["evidence_refs"] = []
    with pytest.raises(ValidationError, match="at least one attribution span"):
        AttributedFinalScorerOutput.model_validate(
            {"dimensions": dimensions, "strengths": [], "priorities": []}
        )


def test_legacy_final_scorer_contract_remains_unchanged() -> None:
    output = FinalScorerOutput.model_validate(
        {
            "dimensions": [
                {
                    "dimension_key": key,
                    "score": None,
                    "quotes": [],
                    "reason": "证据不足。",
                    "confidence": 0.2,
                    "sufficient": False,
                }
                for key in DIMENSION_KEYS
            ],
            "strengths": ["旧版摘要仍是字符串。"],
            "priorities": [],
        }
    )
    assert output.strengths == ["旧版摘要仍是字符串。"]
