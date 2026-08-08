from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from scripts import evaluate_evidence_attribution_gold as evaluator


TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "evidence_attribution_gold_template.jsonl"
)


def _annotation(owner: str, relation: str, eligibility: str) -> dict[str, str]:
    return {
        "owner": owner,
        "relation": relation,
        "eligibility": eligibility,
    }


def _system_prediction(
    annotation: dict[str, str],
    *,
    turn_index: int,
    start: int,
    text: str,
    used_for_scoring: bool,
) -> dict[str, object]:
    return {
        **annotation,
        "abstain": False,
        "turn_index": turn_index,
        "start": start,
        "end": start + len(text),
        "text": text,
        "used_for_scoring": used_for_scoring,
    }


def _abstention() -> dict[str, object]:
    return {
        "owner": None,
        "relation": None,
        "eligibility": None,
        "abstain": True,
        "turn_index": None,
        "start": None,
        "end": None,
        "text": None,
        "used_for_scoring": False,
    }


def _row(
    case_id: str,
    span_id: str,
    text: str,
    annotation: dict[str, str],
    *,
    full_answer: str | None = None,
    turn_index: int = 1,
    start: int = 0,
    system: dict[str, object] | None = None,
    used_for_scoring: bool | None = None,
    expert_scores: dict[str, float] | None = None,
    system_scores: dict[str, float] | None = None,
    equivalence_group: str | None = None,
    equivalence_axis: str | None = None,
    style_variant: str | None = None,
) -> dict[str, Any]:
    answer = text if full_answer is None else full_answer
    if used_for_scoring is None:
        used_for_scoring = annotation["eligibility"] == "eligible"
    result: dict[str, Any] = {
        "case_id": case_id,
        "span_id": span_id,
        "turn_index": turn_index,
        "start": start,
        "end": start + len(text),
        "text": text,
        "full_answer": answer,
        "expert_a": dict(annotation),
        "expert_b": dict(annotation),
        "adjudicated": dict(annotation),
        "system": system
        if system is not None
        else _system_prediction(
            annotation,
            turn_index=turn_index,
            start=start,
            text=text,
            used_for_scoring=used_for_scoring,
        ),
        "expert_scores": expert_scores,
        "system_scores": system_scores,
    }
    if equivalence_group is not None:
        result.update(
            {
                "equivalence_group": equivalence_group,
                "equivalence_axis": equivalence_axis,
                "style_variant": style_variant,
            }
        )
    return result


def _fully_covered_rows() -> list[dict[str, Any]]:
    dimensions = list(evaluator.DIMENSIONS)
    expert_scores = {dimension: 4 for dimension in dimensions}
    system_values = (3, 4, 2, 4, 5, 3)
    system_scores = dict(zip(dimensions, system_values))
    specifications = (
        (
            "owned_reasoning",
            "我会先界定真正需要解决的问题。",
            _annotation("participant_owned", "own_reasoning", "eligible"),
        ),
        (
            "owned_endorsement",
            "我采纳这个建议，因为它能先验证最高风险。",
            _annotation("participant_owned", "endorses", "context_only"),
        ),
        (
            "owned_critique",
            "我不同意，因为样本只覆盖老用户。",
            _annotation("participant_owned", "critiques", "eligible"),
        ),
        (
            "owned_rejection",
            "我会拒绝直接上线，因为新用户风险还没有证据。",
            _annotation("participant_owned", "rejects", "eligible"),
        ),
        (
            "external_quote",
            "论文说要最大化期望效用。",
            _annotation("external_quoted", "quotes_only", "context_only"),
        ),
        (
            "external_paraphrase",
            "报告的大意是应该先追求短期回报。",
            _annotation("external_paraphrased", "endorses", "context_only"),
        ),
        (
            "uncertain_source",
            "这个方案据说更稳，我也觉得也许如此。",
            _annotation("uncertain", "quotes_only", "manual_review"),
        ),
        (
            "asks_ai",
            "我问 AI：你能不能替我比较这两个选择？",
            _annotation("participant_owned", "asks_or_requests", "context_only"),
        ),
    )
    rows: list[dict[str, Any]] = []
    for index, (case_id, text, annotation) in enumerate(specifications):
        rows.append(
            _row(
                case_id,
                "span_01",
                text,
                annotation,
                expert_scores=expert_scores if index == 0 else None,
                system_scores=system_scores if index == 0 else None,
            )
        )

    equivalents = {
        "length": (
            ("short", "我不会直接采纳，因为样本太窄。"),
            ("long", "我不会直接采纳这个建议；现有样本只覆盖老用户，还不足以判断新用户风险。"),
        ),
        "register": (
            ("spoken", "我觉得先别上线吧，新用户会不会踩坑还说不准。"),
            ("written", "我暂不支持上线，因为当前证据尚不足以判断新用户风险。"),
        ),
        "ai_styling": (
            ("human_like", "我会先小范围试一周；投诉变多就停下调整。"),
            ("ai_like", "综合现有条件，我选择先开展一周小范围试行；若投诉数量上升，则暂停并调整。"),
        ),
    }
    equivalence_annotation = _annotation(
        "participant_owned", "own_reasoning", "eligible"
    )
    for axis, variants in equivalents.items():
        for variant, text in variants:
            rows.append(
                _row(
                    f"equivalence_{axis}_{variant}",
                    "span_01",
                    text,
                    equivalence_annotation,
                    equivalence_group=f"owned_reasoning_{axis}",
                    equivalence_axis=axis,
                    style_variant=variant,
                )
            )
    return rows


def test_fully_covered_gold_reports_verified_metrics_and_passed_gates() -> None:
    result = evaluator.evaluate_rows(_fully_covered_rows())

    assert result["protocol"] == "evidence-attribution-gold-v2"
    assert result["status"] == "VERIFIED"
    assert result["acceptance_gates"]["coverage_passed"] is True
    assert result["acceptance_gates"]["safety_passed"] is True
    assert result["acceptance_gates"]["overall_passed"] is True
    metrics = result["metrics"]
    assert metrics["macro_f1"] == 1.0
    assert all(
        item["gold_label_support_complete"]
        for item in metrics["macro_f1_by_label"].values()
    )
    assert metrics["external_text_false_acceptance_rate"]["value"] == 0.0
    assert metrics["uncertain_auto_scoring_rate"]["value"] == 0.0
    assert metrics["unsafe_noneligible_auto_scoring_rate"]["value"] == 0.0
    assert metrics["participant_reasoning_miss_rate"]["value"] == 0.0
    assert metrics["exact_span_recovery_rate"]["value"] == 1.0
    assert metrics["expert_agreement"]["macro_kappa_status"] == "VERIFIED"
    assert metrics["dimension_mae"]["problem_definition"]["mae"] == 1.0
    assert metrics["dimension_mae"]["reasoning_argumentation"]["mae"] == 2.0
    disparity = metrics["content_equivalence_disparity"]
    assert disparity["status"] == "VERIFIED"
    assert disparity["present_axes"] == ["ai_styling", "length", "register"]
    assert disparity["aggregation"] == "macro_average_across_groups"
    assert disparity["outcome_disagreement_rate"] == 0.0


def test_wrong_occurrence_is_exact_span_miss_even_when_labels_are_correct() -> None:
    rows = _fully_covered_rows()
    target = next(row for row in rows if row["case_id"] == "owned_critique")
    text = target["text"]
    target["full_answer"] = text + text
    target["system"].update(
        {
            "start": len(text),
            "end": len(text) * 2,
            "text": text,
        }
    )

    result = evaluator.evaluate_rows(rows)

    assert result["status"] == "VERIFIED"
    assert result["metrics"]["participant_reasoning_miss_rate"]["numerator"] == 1
    assert result["metrics"]["exact_span_recovery_rate"]["numerator"] == len(rows) - 1
    assert result["metrics"]["macro_f1"] < 1.0
    assert result["metrics"]["macro_f1_by_label"]["owner"][
        "span_mismatches_counted_as_misses"
    ] is True


def test_auto_scoring_rates_use_used_for_scoring_and_fail_safety_gates() -> None:
    rows = _fully_covered_rows()
    external = next(row for row in rows if row["case_id"] == "external_quote")
    uncertain = next(row for row in rows if row["case_id"] == "uncertain_source")
    external["system"]["used_for_scoring"] = True
    uncertain["system"]["used_for_scoring"] = True

    result = evaluator.evaluate_rows(rows)

    assert result["status"] == "VERIFIED"
    assert result["acceptance_gates"]["coverage_passed"] is True
    assert result["acceptance_gates"]["safety_passed"] is False
    assert result["acceptance_gates"]["overall_passed"] is False
    assert result["metrics"]["external_text_false_acceptance_rate"] == {
        "value": 0.5,
        "numerator": 1,
        "denominator": 2,
        "status": "VERIFIED",
    }
    assert result["metrics"]["uncertain_auto_scoring_rate"]["value"] == 1.0
    assert result["metrics"]["unsafe_noneligible_auto_scoring_rate"]["numerator"] == 2


def test_complete_but_undercovered_dataset_is_partial_with_fixed_label_support() -> None:
    annotation = _annotation("participant_owned", "own_reasoning", "eligible")
    rows = [_row("only_case", "span_01", "我会先核实来源。", annotation)]

    result = evaluator.evaluate_rows(rows)

    assert result["status"] == "PARTIAL"
    assert result["reason"] == "acceptance_coverage_incomplete"
    assert result["acceptance_gates"]["coverage_passed"] is False
    coverage = result["acceptance_gates"]["coverage"]["gates"]
    assert coverage["external_owner_coverage"]["passed"] is False
    assert coverage["uncertain_owner_coverage"]["passed"] is False
    assert coverage["equivalence_axes_coverage"]["passed"] is False
    assert coverage["six_dimension_paired_score_coverage"]["passed"] is False
    owner_f1 = result["metrics"]["macro_f1_by_label"]["owner"]
    assert owner_f1["value"] == 0.25
    assert owner_f1["gold_support"]["participant_owned"] == 1
    assert set(owner_f1["missing_gold_labels"]) == {
        "external_paraphrased",
        "external_quoted",
        "uncertain",
    }


def test_gold_span_contract_rejects_bad_slice_overlap_and_missing_fields() -> None:
    annotation = _annotation("participant_owned", "own_reasoning", "eligible")
    missing_answer = _row("missing", "span_01", "abc", annotation)
    del missing_answer["full_answer"]
    with pytest.raises(
        evaluator.DatasetValidationError,
        match=r"missing_required_field:row\[1\]\.full_answer",
    ):
        evaluator.evaluate_rows([missing_answer])

    bad_slice = _row("bad_slice", "span_01", "abc", annotation)
    bad_slice["start"] = 1
    bad_slice["end"] = 4
    with pytest.raises(
        evaluator.DatasetValidationError,
        match="gold_span_slice_mismatch:bad_slice/span_01",
    ):
        evaluator.evaluate_rows([bad_slice])

    full_answer = "abcd"
    first = _row(
        "overlap", "span_01", "abc", annotation, full_answer=full_answer, start=0
    )
    second = _row(
        "overlap", "span_02", "bcd", annotation, full_answer=full_answer, start=1
    )
    with pytest.raises(
        evaluator.DatasetValidationError,
        match="overlapping_gold_spans:overlap:turn=1:span_01:span_02",
    ):
        evaluator.evaluate_rows([first, second])


def test_completed_system_prediction_requires_exact_coordinate_contract() -> None:
    annotation = _annotation("participant_owned", "own_reasoning", "eligible")
    row = _row("missing_system_coordinate", "span_01", "abc", annotation)
    del row["system"]["start"]
    with pytest.raises(
        evaluator.DatasetValidationError,
        match=r"missing_required_field:missing_system_coordinate/span_01\.system\.start",
    ):
        evaluator.evaluate_rows([row])

    row = _row("bad_system_slice", "span_01", "abc", annotation, full_answer="abcxyz")
    row["system"].update({"start": 3, "end": 6, "text": "bad"})
    with pytest.raises(
        evaluator.DatasetValidationError,
        match="system_span_slice_mismatch:bad_system_slice/span_01",
    ):
        evaluator.evaluate_rows([row])


def test_expert_and_adjudicated_eligibility_matrix_fails_closed() -> None:
    valid_contract = (
        ("uncertain", "own_reasoning", "manual_review"),
        ("external_quoted", "quotes_only", "context_only"),
        ("external_paraphrased", "endorses", "context_only"),
        ("participant_owned", "own_reasoning", "eligible"),
        ("participant_owned", "critiques", "eligible"),
        ("participant_owned", "rejects", "eligible"),
        ("participant_owned", "endorses", "context_only"),
        ("participant_owned", "quotes_only", "context_only"),
        ("participant_owned", "asks_or_requests", "context_only"),
    )
    for index, values in enumerate(valid_contract):
        annotation = _annotation(*values)
        result = evaluator.evaluate_rows(
            [_row(f"valid_matrix_{index}", "span_01", f"样例 {index}", annotation)]
        )
        assert result["status"] == "PARTIAL"

    invalid_contract = (
        ("uncertain", "own_reasoning", "context_only"),
        ("external_quoted", "quotes_only", "eligible"),
        ("external_paraphrased", "endorses", "manual_review"),
        ("participant_owned", "own_reasoning", "context_only"),
        ("participant_owned", "critiques", "context_only"),
        ("participant_owned", "rejects", "context_only"),
        ("participant_owned", "endorses", "eligible"),
        ("participant_owned", "quotes_only", "eligible"),
        ("participant_owned", "asks_or_requests", "eligible"),
    )
    for index, values in enumerate(invalid_contract):
        annotation = _annotation(*values)
        row = _row(f"invalid_matrix_{index}", "span_01", f"非法 {index}", annotation)
        with pytest.raises(
            evaluator.DatasetValidationError,
            match=rf"invalid_gold_eligibility_matrix:invalid_matrix_{index}/span_01\.expert_a",
        ):
            evaluator.evaluate_rows([row])


def test_equivalence_requires_same_gold_and_reports_each_output_field() -> None:
    rows = _fully_covered_rows()
    length_rows = [
        row for row in rows if row.get("equivalence_axis") == "length"
    ]
    length_rows[1]["system"].update(
        {
            "owner": "external_quoted",
            "relation": "quotes_only",
            "eligibility": "eligible",
        }
    )
    register_rows = [
        row for row in rows if row.get("equivalence_axis") == "register"
    ]
    register_rows[1]["system"] = _abstention()

    disparity = evaluator.evaluate_rows(rows)["metrics"][
        "content_equivalence_disparity"
    ]

    assert disparity["groups"]["owned_reasoning_length"]["owner_disagreement_rate"] == 1.0
    assert disparity["groups"]["owned_reasoning_length"]["relation_disagreement_rate"] == 1.0
    assert disparity["groups"]["owned_reasoning_length"]["eligibility_disagreement_rate"] == 0.0
    register = disparity["groups"]["owned_reasoning_register"]
    assert register["abstention_disagreement_rate"] == 1.0
    assert register["label_pair_count"] == 0
    assert register["eligibility_disagreement_rate"] is None

    invalid_rows = _fully_covered_rows()
    invalid_pair = [
        row for row in invalid_rows if row.get("equivalence_axis") == "length"
    ]
    invalid_pair[1]["adjudicated"] = _annotation(
        "participant_owned", "critiques", "eligible"
    )
    with pytest.raises(
        evaluator.DatasetValidationError,
        match="equivalence_group_adjudicated_labels_differ:owned_reasoning_length",
    ):
        evaluator.evaluate_rows(invalid_rows)


def test_conflicting_or_unpaired_case_scores_do_not_create_verified_mae() -> None:
    rows = _fully_covered_rows()
    duplicate = copy.deepcopy(rows[0])
    duplicate["span_id"] = "span_02"
    duplicate["start"] = len(rows[0]["text"])
    duplicate["end"] = duplicate["start"] + len(rows[0]["text"])
    duplicate["full_answer"] = rows[0]["text"] * 2
    rows[0]["full_answer"] = duplicate["full_answer"]
    duplicate["system"].update(
        {
            "start": duplicate["start"],
            "end": duplicate["end"],
        }
    )
    duplicate["system_scores"] = {
        **duplicate["system_scores"],
        "problem_definition": 5,
    }
    with pytest.raises(
        evaluator.DatasetValidationError,
        match="inconsistent_duplicate_case_score:owned_reasoning:problem_definition",
    ):
        evaluator.evaluate_rows([*rows, duplicate])

    rows = _fully_covered_rows()
    score_row = next(row for row in rows if row["expert_scores"])
    score_row["system_scores"].pop("dynamic_adjustment")
    result = evaluator.evaluate_rows(rows)
    assert result["status"] == "PARTIAL"
    assert result["metrics"]["dimension_mae"]["dynamic_adjustment"]["status"] == (
        "NOT_VERIFIED_UNPAIRED_SCORES"
    )


def test_no_variance_kappa_is_explicitly_partial() -> None:
    annotation = _annotation("participant_owned", "own_reasoning", "eligible")
    rows = [
        _row("same_1", "span_01", "我会核实。", annotation),
        _row("same_2", "span_01", "我会比较。", annotation),
    ]

    result = evaluator.evaluate_rows(rows)

    agreement = result["metrics"]["expert_agreement"]
    assert result["status"] == "PARTIAL"
    assert agreement["macro_kappa_status"] == "PARTIAL_UNDEFINED_NO_VARIANCE"
    assert set(agreement["undefined_kappa_fields"]) == set(evaluator.LABEL_FIELDS)
    assert result["acceptance_gates"]["coverage"]["gates"][
        "expert_kappa_defined"
    ]["passed"] is False


def test_pending_template_has_exact_offsets_and_no_invented_gold_labels() -> None:
    rows = evaluator.load_jsonl(TEMPLATE_PATH)

    assert len(rows) == 19
    assert len([row for row in rows if row["case_id"] == "case_15_mixed_ai_paper"]) == 3
    equivalence_rows = [row for row in rows if row.get("equivalence_group")]
    assert {row["equivalence_axis"] for row in equivalence_rows} == {
        "length",
        "register",
        "ai_styling",
    }
    assert len({row["equivalence_group"] for row in equivalence_rows}) == 3
    for row in rows:
        assert row["full_answer"][row["start"] : row["end"]] == row["text"]
        for block_name in ("expert_a", "expert_b", "adjudicated"):
            assert all(row[block_name][field] is None for field in evaluator.LABEL_FIELDS)
    result = evaluator.evaluate_rows(rows)
    assert result["status"] == "NOT_VERIFIED"
    assert result["acceptance_gates"]["coverage_passed"] is False
    assert all(value is None for value in result["metrics"].values())
    assert evaluator.main([str(TEMPLATE_PATH)]) == 2
