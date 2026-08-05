"""Frozen evidence-scoring contract for the V6 independent final scorer.

The interviewer deliberately receives only the concise dimension descriptions.
The independent scorer, however, must receive the complete observable-behaviour
and 1--5 BARS contract.  A checked-in digest makes an accidental catalog edit a
hard stop for real scoring instead of a silent change to the measurement rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.domain.catalog import DIMENSIONS


FINAL_SCORER_BARS_CONTRACT_VERSION = "v6.2.0"

# These rules turn the five prose anchors into one reproducible ordinal
# decision procedure.  They are part of the frozen contract and therefore of
# its digest; changing any rule requires an intentional version/hash bump.
FINAL_SCORER_ADJUDICATION_RULES: tuple[tuple[str, str], ...] = (
    (
        "eligible_evidence",
        "只使用冻结逐字稿中可精确引用的用户原话，并在该维度上综合审查全部合格证据。",
    ),
    (
        "observation_opportunity",
        "只有当对话曾直接引出或用户曾实质回应该维度的可观察行为时，opportunity_observed 才为 true；仅有泛化话题、自我标签或未曾触及时为 false。",
    ),
    (
        "highest_fully_satisfied",
        "从 5 级向 1 级逐级检查；只有当某锚点的全部语义要件均被合格证据支持且没有未解决的实质反证时，才可选择该最高完全满足等级。",
    ),
    (
        "partial_or_mixed_evidence",
        "部分满足高级锚点不得向上取整；混合证据应选择全部证据共同支持的最高完全锚点，若连任一完全锚点都不足以支持则返回 null。",
    ),
    (
        "cross_turn_conflict",
        "跨轮证据冲突时必须保留冲突；未解决的冲突证据不得用来满足其所争议的锚点，只能依据排除该冲突后仍然充分的证据判级，否则返回 null。",
    ),
    (
        "recency",
        "新近表述不会因更新而自动覆盖旧证据；只有用户明确表示修正原判断，并给出新理由或新信息时，才以修正后的证据为准。",
    ),
    (
        "level_one_vs_insufficient",
        "1 级必须由用户在已有充分观察机会的情况下明确呈现该一级行为才能判定；没有回答、未提及、简短片段或未获充分观察机会均必须 score=null，不得判1分。",
    ),
    (
        "insufficient_output",
        "任一维度在证据不足、仅有自我标签、缺乏观察机会或无法解决冲突时，必须返回 score=null、sufficient=false、quotes=[]，且不得从其他维度借用证据。",
    ),
)

# Updated only after the complete contract below has been intentionally reviewed.
# The digest covers the contract version and canonical six-dimension payload.
FINAL_SCORER_BARS_CONTRACT_SHA256 = (
    "7f0cc87f0b3e0a6cae5b8c65e3208d6be2944bff194f6d540060f16290c77984"
)


class FinalScorerContractError(RuntimeError):
    """The checked-in scoring contract is incomplete or no longer frozen."""


def final_scorer_bars_contract_payload() -> dict[str, Any]:
    """Return the deterministic, JSON-serializable scoring contract payload."""

    dimensions: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        if set(dimension.bars) != set(range(1, 6)):
            raise FinalScorerContractError(
                f"catalog_bars_levels_changed:{dimension.key}"
            )
        dimensions.append(
            {
                "dimension_key": dimension.key,
                "dimension_name": dimension.name,
                "description": dimension.description,
                "observable_behaviors": list(dimension.observable_behaviors),
                "bars": [
                    {"level": level, "anchor": dimension.bars.get(level)}
                    for level in range(1, 6)
                ],
            }
        )
    return {
        "contract_version": FINAL_SCORER_BARS_CONTRACT_VERSION,
        "adjudication_rules": [
            {"rule_key": key, "rule": rule}
            for key, rule in FINAL_SCORER_ADJUDICATION_RULES
        ],
        "dimensions": dimensions,
    }


def _validate_contract_shape(payload: dict[str, Any]) -> None:
    version = payload.get("contract_version")
    if not isinstance(version, str) or not version.strip():
        raise FinalScorerContractError("missing_contract_version")

    adjudication_rules = payload.get("adjudication_rules")
    expected_rule_keys = [key for key, _ in FINAL_SCORER_ADJUDICATION_RULES]
    if not isinstance(adjudication_rules, list) or [
        item.get("rule_key") for item in adjudication_rules
    ] != expected_rule_keys:
        raise FinalScorerContractError("adjudication_rule_order_or_membership_changed")
    if any(
        not isinstance(item.get("rule"), str) or not item["rule"].strip()
        for item in adjudication_rules
    ):
        raise FinalScorerContractError("missing_adjudication_rule")

    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != 6:
        raise FinalScorerContractError("expected_six_dimensions")

    expected_keys = [dimension.key for dimension in DIMENSIONS]
    actual_keys = [item.get("dimension_key") for item in dimensions]
    if actual_keys != expected_keys or len(set(actual_keys)) != 6:
        raise FinalScorerContractError("dimension_key_order_or_membership_changed")

    for item in dimensions:
        for field in ("dimension_key", "dimension_name", "description"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise FinalScorerContractError(
                    f"missing_dimension_field:{item.get('dimension_key')}:{field}"
                )

        behaviors = item.get("observable_behaviors")
        if not isinstance(behaviors, list) or not behaviors or any(
            not isinstance(value, str) or not value.strip() for value in behaviors
        ):
            raise FinalScorerContractError(
                f"missing_observable_behaviors:{item.get('dimension_key')}"
            )

        bars = item.get("bars")
        if not isinstance(bars, list) or [bar.get("level") for bar in bars] != list(
            range(1, 6)
        ):
            raise FinalScorerContractError(
                f"expected_bars_levels_1_to_5:{item.get('dimension_key')}"
            )
        if any(
            not isinstance(bar.get("anchor"), str) or not bar["anchor"].strip()
            for bar in bars
        ):
            raise FinalScorerContractError(
                f"missing_bars_anchor:{item.get('dimension_key')}"
            )


def canonical_final_scorer_bars_contract_json() -> str:
    payload = final_scorer_bars_contract_payload()
    _validate_contract_shape(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def calculate_final_scorer_bars_contract_sha256() -> str:
    canonical = canonical_final_scorer_bars_contract_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assert_final_scorer_bars_contract_integrity() -> str:
    """Return the frozen digest or reject a missing/drifted real-score contract."""

    expected = FINAL_SCORER_BARS_CONTRACT_SHA256
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise FinalScorerContractError("missing_or_invalid_frozen_contract_sha256")
    actual = calculate_final_scorer_bars_contract_sha256()
    if actual != expected:
        raise FinalScorerContractError(
            f"frozen_contract_sha256_mismatch:expected={expected}:actual={actual}"
        )
    return actual


def render_final_scorer_bars_contract() -> str:
    """Render the complete frozen contract for the independent scorer prompt."""

    payload = final_scorer_bars_contract_payload()
    _validate_contract_shape(payload)
    digest = calculate_final_scorer_bars_contract_sha256()
    lines = [
        f"合同版本：{payload['contract_version']}",
        f"合同 SHA256：{digest}",
        "",
        "裁决规则（按顺序执行）：",
    ]
    lines.extend(
        f"{index}. {item['rule_key']}：{item['rule']}"
        for index, item in enumerate(payload["adjudication_rules"], start=1)
    )
    for dimension in payload["dimensions"]:
        lines.extend(
            [
                "",
                (
                    f"- {dimension['dimension_key']}（{dimension['dimension_name']}）："
                    f"{dimension['description']}"
                ),
                "  可观察行为：" + "；".join(dimension["observable_behaviors"]),
            ]
        )
        lines.extend(
            f"  BARS {bar['level']}：{bar['anchor']}" for bar in dimension["bars"]
        )
    return "\n".join(lines)
