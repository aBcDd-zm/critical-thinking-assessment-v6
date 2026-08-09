#!/usr/bin/env python3
"""Evaluate a fully adjudicated evidence-attribution gold JSONL file.

The evaluator is deliberately independent from application code and model
providers.  It never fills absent expert annotations and never reports
metrics for an incompletely labelled dataset.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Sequence


PROTOCOL = "evidence-attribution-gold-v2"

LABEL_FIELDS = ("owner", "relation", "eligibility")
OWNER_LABELS = frozenset(
    {
        "participant_owned",
        "external_quoted",
        "external_paraphrased",
        "uncertain",
    }
)
RELATION_LABELS = frozenset(
    {
        "own_reasoning",
        "endorses",
        "critiques",
        "rejects",
        "quotes_only",
        "asks_or_requests",
    }
)
ELIGIBILITY_LABELS = frozenset({"eligible", "context_only", "manual_review"})
LABEL_SETS = {
    "owner": OWNER_LABELS,
    "relation": RELATION_LABELS,
    "eligibility": ELIGIBILITY_LABELS,
}
EXPERT_BLOCKS = ("expert_a", "expert_b", "adjudicated")
EXTERNAL_OWNERS = frozenset({"external_quoted", "external_paraphrased"})
DIMENSIONS = (
    "problem_definition",
    "evidence_evaluation",
    "reasoning_argumentation",
    "multiple_perspectives",
    "integrative_decision",
    "dynamic_adjustment",
)
EQUIVALENCE_AXES = frozenset({"length", "register", "ai_styling"})
SCORABLE_RELATIONS = frozenset(
    {"own_reasoning", "critiques", "rejects"}
)
SYSTEM_PREDICTION_FIELDS = (
    "turn_index",
    "start",
    "end",
    "text",
    "used_for_scoring",
)


class DatasetValidationError(ValueError):
    """Raised when the file shape is unsafe to evaluate."""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load non-empty JSONL records with line-numbered validation errors."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetValidationError(f"cannot_read_jsonl:{path}") from exc

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(
                f"invalid_jsonl_line:{line_number}:{exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise DatasetValidationError(f"jsonl_row_must_be_object:{line_number}")
        rows.append(value)
    if not rows:
        raise DatasetValidationError("gold_dataset_is_empty")
    return rows


def _row_ref(row: dict[str, Any]) -> str:
    return f"{row.get('case_id', '?')}/{row.get('span_id', '?')}"


def _require_key(mapping: dict[str, Any], key: str, *, where: str) -> Any:
    if key not in mapping:
        raise DatasetValidationError(f"missing_required_field:{where}.{key}")
    return mapping[key]


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _validate_label(value: Any, field: str, *, where: str) -> None:
    if _is_blank(value):
        return
    if not isinstance(value, str) or value not in LABEL_SETS[field]:
        allowed = ",".join(sorted(LABEL_SETS[field]))
        raise DatasetValidationError(
            f"invalid_label:{where}.{field}:{value!r}:allowed={allowed}"
        )


def _validate_score_mapping(value: Any, *, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise DatasetValidationError(f"score_mapping_must_be_object:{where}")
    unknown = set(value) - set(DIMENSIONS)
    if unknown:
        raise DatasetValidationError(
            f"unknown_score_dimension:{where}:{','.join(sorted(unknown))}"
        )
    for dimension, score in value.items():
        if score is None:
            continue
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 1 <= score <= 5
        ):
            raise DatasetValidationError(
                f"invalid_score:{where}.{dimension}:{score!r}:expected_1_to_5"
            )


def _require_nonnegative_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetValidationError(f"nonnegative_integer_required:{where}:{value!r}")
    return value


def _validate_gold_eligibility_matrix(
    block: dict[str, Any], *, where: str
) -> None:
    """Validate the frozen expert contract without constraining system errors."""

    if any(_is_blank(block[field]) for field in LABEL_FIELDS):
        return
    owner = block["owner"]
    relation = block["relation"]
    eligibility = block["eligibility"]
    if owner == "uncertain":
        expected_eligibility = "manual_review"
    elif owner in EXTERNAL_OWNERS:
        expected_eligibility = "context_only"
    elif relation in SCORABLE_RELATIONS:
        expected_eligibility = "eligible"
    else:
        expected_eligibility = "context_only"
    if eligibility != expected_eligibility:
        raise DatasetValidationError(
            f"invalid_gold_eligibility_matrix:{where}:"
            f"owner={owner}:relation={relation}:eligibility={eligibility}:"
            f"expected={expected_eligibility}"
        )


def _validate_system_prediction(
    row: dict[str, Any], system: dict[str, Any], *, ref: str
) -> tuple[int, int, int, str] | None:
    """Validate a completed prediction and return its declared span identity."""

    abstain = system["abstain"]
    for field in SYSTEM_PREDICTION_FIELDS:
        _require_key(system, field, where=f"{ref}.system")

    used_for_scoring = system["used_for_scoring"]
    if not isinstance(used_for_scoring, bool):
        raise DatasetValidationError(
            f"system_used_for_scoring_must_be_boolean:{ref}"
        )

    if abstain:
        populated = [
            field
            for field in ("turn_index", "start", "end", "text")
            if system[field] is not None
        ]
        if populated:
            raise DatasetValidationError(
                f"abstained_system_span_must_be_null:{ref}:{','.join(populated)}"
            )
        if used_for_scoring:
            raise DatasetValidationError(
                f"abstained_system_cannot_be_used_for_scoring:{ref}"
            )
        return None

    turn_index = _require_nonnegative_int(
        system["turn_index"], where=f"{ref}.system.turn_index"
    )
    start = _require_nonnegative_int(system["start"], where=f"{ref}.system.start")
    end = _require_nonnegative_int(system["end"], where=f"{ref}.system.end")
    text = system["text"]
    if not isinstance(text, str) or not text:
        raise DatasetValidationError(f"system_text_must_be_nonempty_string:{ref}")
    if end <= start or end - start != len(text):
        raise DatasetValidationError(
            f"invalid_system_span_bounds:{ref}:start={start}:end={end}:text_length={len(text)}"
        )
    if turn_index == row["turn_index"]:
        full_answer = row["full_answer"]
        if end > len(full_answer) or full_answer[start:end] != text:
            raise DatasetValidationError(
                f"system_span_slice_mismatch:{ref}:start={start}:end={end}"
            )
    return turn_index, start, end, text


def _validate_non_overlapping_spans(
    grouped: dict[tuple[str, int], list[tuple[int, int, str]]], *, kind: str
) -> None:
    for (case_id, turn_index), spans in grouped.items():
        previous: tuple[int, int, str] | None = None
        for span in sorted(spans):
            if previous is not None and span[0] < previous[1]:
                raise DatasetValidationError(
                    f"overlapping_{kind}_spans:{case_id}:turn={turn_index}:"
                    f"{previous[2]}:{span[2]}"
                )
            previous = span


def _validate_structure(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Validate exact span/data contracts and return annotations still blank."""

    seen: set[tuple[str, str]] = set()
    missing_annotations: list[str] = []
    answers_by_case_turn: dict[tuple[str, int], str] = {}
    gold_spans: dict[tuple[str, int], list[tuple[int, int, str]]] = {}
    system_spans: dict[tuple[str, int], list[tuple[int, int, str]]] = {}

    for row_number, row in enumerate(rows, start=1):
        case_id = _require_key(row, "case_id", where=f"row[{row_number}]")
        span_id = _require_key(row, "span_id", where=f"row[{row_number}]")
        text = _require_key(row, "text", where=f"row[{row_number}]")
        full_answer = _require_key(row, "full_answer", where=f"row[{row_number}]")
        turn_index = _require_nonnegative_int(
            _require_key(row, "turn_index", where=f"row[{row_number}]"),
            where=f"row[{row_number}].turn_index",
        )
        start = _require_nonnegative_int(
            _require_key(row, "start", where=f"row[{row_number}]"),
            where=f"row[{row_number}].start",
        )
        end = _require_nonnegative_int(
            _require_key(row, "end", where=f"row[{row_number}]"),
            where=f"row[{row_number}].end",
        )
        if not isinstance(case_id, str) or not case_id.strip():
            raise DatasetValidationError(f"case_id_must_be_nonempty_string:{row_number}")
        if (
            isinstance(span_id, bool)
            or not isinstance(span_id, (str, int))
            or (isinstance(span_id, str) and not span_id.strip())
        ):
            raise DatasetValidationError(f"span_id_must_be_string_or_integer:{row_number}")
        if not isinstance(text, str) or not text:
            raise DatasetValidationError(f"text_must_be_nonempty_string:{row_number}")
        if not isinstance(full_answer, str) or not full_answer:
            raise DatasetValidationError(f"full_answer_must_be_nonempty_string:{row_number}")
        if end <= start or end > len(full_answer) or full_answer[start:end] != text:
            raise DatasetValidationError(
                f"gold_span_slice_mismatch:{case_id}/{span_id}:start={start}:end={end}"
            )

        normalized_case_id = case_id.strip()
        identity = (normalized_case_id, str(span_id))
        if identity in seen:
            raise DatasetValidationError(f"duplicate_case_span:{identity[0]}/{identity[1]}")
        seen.add(identity)

        case_turn = (normalized_case_id, turn_index)
        previous_answer = answers_by_case_turn.setdefault(case_turn, full_answer)
        if previous_answer != full_answer:
            raise DatasetValidationError(
                f"inconsistent_full_answer:{normalized_case_id}:turn={turn_index}"
            )
        gold_spans.setdefault(case_turn, []).append((start, end, str(span_id)))

        ref = _row_ref(row)
        for block_name in EXPERT_BLOCKS:
            block = _require_key(row, block_name, where=ref)
            if not isinstance(block, dict):
                raise DatasetValidationError(
                    f"annotation_block_must_be_object:{ref}.{block_name}"
                )
            for field in LABEL_FIELDS:
                value = _require_key(block, field, where=f"{ref}.{block_name}")
                _validate_label(value, field, where=f"{ref}.{block_name}")
                if _is_blank(value):
                    missing_annotations.append(f"{ref}:{block_name}.{field}")
            _validate_gold_eligibility_matrix(block, where=f"{ref}.{block_name}")

        system = _require_key(row, "system", where=ref)
        if not isinstance(system, dict):
            raise DatasetValidationError(f"annotation_block_must_be_object:{ref}.system")
        abstain = _require_key(system, "abstain", where=f"{ref}.system")
        if abstain is not None and not isinstance(abstain, bool):
            raise DatasetValidationError(f"system_abstain_must_be_boolean_or_null:{ref}")
        for field in LABEL_FIELDS:
            value = _require_key(system, field, where=f"{ref}.system")
            _validate_label(value, field, where=f"{ref}.system")

        if abstain is None:
            missing_annotations.append(f"{ref}:system.abstain")
        elif abstain:
            populated = [field for field in LABEL_FIELDS if not _is_blank(system[field])]
            if populated:
                raise DatasetValidationError(
                    f"abstained_system_labels_must_be_null:{ref}:{','.join(populated)}"
                )
            _validate_system_prediction(row, system, ref=ref)
        else:
            for field in LABEL_FIELDS:
                if _is_blank(system[field]):
                    missing_annotations.append(f"{ref}:system.{field}")
            prediction = _validate_system_prediction(row, system, ref=ref)
            if prediction is not None:
                predicted_turn, predicted_start, predicted_end, _ = prediction
                system_spans.setdefault(
                    (normalized_case_id, predicted_turn), []
                ).append((predicted_start, predicted_end, str(span_id)))

        _validate_score_mapping(row.get("expert_scores"), where=f"{ref}.expert_scores")
        _validate_score_mapping(row.get("system_scores"), where=f"{ref}.system_scores")

    _validate_non_overlapping_spans(gold_spans, kind="gold")
    _validate_non_overlapping_spans(system_spans, kind="system")
    _validate_equivalence_groups(rows)
    return missing_annotations


def _validate_equivalence_groups(rows: Sequence[dict[str, Any]]) -> None:
    """Validate paired metadata and adjudicated content-equivalence invariants."""

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = row.get("equivalence_group")
        axis = row.get("equivalence_axis")
        variant = row.get("style_variant")
        values = (group, axis, variant)
        if all(value is None for value in values):
            continue
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise DatasetValidationError(
                f"incomplete_equivalence_metadata:{_row_ref(row)}"
            )
        if axis not in EQUIVALENCE_AXES:
            allowed = ",".join(sorted(EQUIVALENCE_AXES))
            raise DatasetValidationError(
                f"invalid_equivalence_axis:{_row_ref(row)}:{axis}:allowed={allowed}"
            )
        entry = groups.setdefault(
            group, {"axes": set(), "variants": set(), "members": []}
        )
        entry["axes"].add(axis)
        if variant in entry["variants"]:
            raise DatasetValidationError(
                f"duplicate_equivalence_variant:{group}:{variant}"
            )
        entry["variants"].add(variant)
        entry["members"].append(row)

    for group, entry in groups.items():
        if len(entry["axes"]) != 1:
            raise DatasetValidationError(f"mixed_equivalence_axes:{group}")
        if len(entry["members"]) < 2:
            raise DatasetValidationError(f"equivalence_group_requires_pair:{group}")
        adjudicated = [
            tuple(row["adjudicated"][field] for field in LABEL_FIELDS)
            for row in entry["members"]
        ]
        if all(not any(_is_blank(value) for value in labels) for labels in adjudicated):
            if len(set(adjudicated)) != 1:
                raise DatasetValidationError(
                    f"equivalence_group_adjudicated_labels_differ:{group}"
                )


def _round(value: float) -> float:
    return round(value, 6)


def _agreement_and_kappa(
    left: Sequence[str], right: Sequence[str]
) -> dict[str, Any]:
    sample_count = len(left)
    if len(left) != len(right):
        raise DatasetValidationError("agreement_sequences_have_different_lengths")
    observed = sum(a == b for a, b in zip(left, right)) / sample_count
    left_counts = Counter(left)
    right_counts = Counter(right)
    categories = set(left_counts) | set(right_counts)
    expected = sum(
        (left_counts[label] / sample_count) * (right_counts[label] / sample_count)
        for label in categories
    )
    if math.isclose(expected, 1.0):
        kappa = None
        kappa_status = "UNDEFINED_NO_VARIANCE"
    else:
        kappa = _round((observed - expected) / (1.0 - expected))
        kappa_status = "VERIFIED"
    return {
        "sample_count": sample_count,
        "agreement_rate": _round(observed),
        "cohen_kappa": kappa,
        "kappa_status": kappa_status,
    }


def _expert_agreement(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    for field in LABEL_FIELDS:
        fields[field] = _agreement_and_kappa(
            [row["expert_a"][field] for row in rows],
            [row["expert_b"][field] for row in rows],
        )
    kappas = [
        result["cohen_kappa"]
        for result in fields.values()
        if result["cohen_kappa"] is not None
    ]
    undefined_fields = [
        field
        for field, result in fields.items()
        if result["cohen_kappa"] is None
    ]
    joint = sum(
        all(row["expert_a"][field] == row["expert_b"][field] for field in LABEL_FIELDS)
        for row in rows
    ) / len(rows)
    return {
        "by_label": fields,
        "joint_exact_match_rate": _round(joint),
        "macro_agreement_rate": _round(
            fmean(result["agreement_rate"] for result in fields.values())
        ),
        "macro_cohen_kappa": _round(fmean(kappas)) if kappas else None,
        "macro_kappa_status": (
            "VERIFIED"
            if not undefined_fields
            else "PARTIAL_UNDEFINED_NO_VARIANCE"
        ),
        "defined_kappa_field_count": len(kappas),
        "undefined_kappa_fields": undefined_fields,
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "value": _round(numerator / denominator) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "status": "VERIFIED" if denominator else "NOT_APPLICABLE_NO_GOLD_CASES",
    }


def _system_exact_span_match(row: dict[str, Any]) -> bool:
    system = row["system"]
    return bool(
        not system["abstain"]
        and system["turn_index"] == row["turn_index"]
        and system["start"] == row["start"]
        and system["end"] == row["end"]
        and system["text"] == row["text"]
    )


def _macro_f1(
    gold: Sequence[str],
    predicted: Sequence[str | None],
    *,
    labels: Sequence[str],
) -> dict[str, Any]:
    if len(gold) != len(predicted):
        raise DatasetValidationError("classification_sequences_have_different_lengths")
    per_label: dict[str, float] = {}
    gold_support: dict[str, int] = {}
    predicted_support: dict[str, int] = {}
    confusion: dict[str, dict[str, int]] = {}
    for label in labels:
        true_positive = sum(
            expected == label and actual == label
            for expected, actual in zip(gold, predicted)
        )
        false_positive = sum(
            expected != label and actual == label
            for expected, actual in zip(gold, predicted)
        )
        false_negative = sum(
            expected == label and actual != label
            for expected, actual in zip(gold, predicted)
        )
        gold_support[label] = sum(expected == label for expected in gold)
        predicted_support[label] = sum(actual == label for actual in predicted)
        denominator = (2 * true_positive) + false_positive + false_negative
        per_label[label] = _round((2 * true_positive) / denominator) if denominator else 0.0
        confusion[label] = {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
        }
    missing_gold_labels = [label for label in labels if gold_support[label] == 0]
    return {
        "value": _round(fmean(per_label.values())),
        "per_label": per_label,
        "gold_support": gold_support,
        "predicted_support": predicted_support,
        "confusion": confusion,
        "missing_gold_labels": missing_gold_labels,
        "gold_label_support_complete": not missing_gold_labels,
        "label_universe": list(labels),
        "sample_count": len(gold),
        "abstentions_counted_as_misses": True,
        "span_mismatches_counted_as_misses": True,
    }


def _classification_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_label: dict[str, dict[str, Any]] = {}
    for field in LABEL_FIELDS:
        gold = [row["adjudicated"][field] for row in rows]
        predicted = [
            (
                None
                if row["system"]["abstain"] or not _system_exact_span_match(row)
                else row["system"][field]
            )
            for row in rows
        ]
        by_label[field] = _macro_f1(
            gold,
            predicted,
            labels=sorted(LABEL_SETS[field]),
        )
    return {
        "by_label": by_label,
        "macro_f1": _round(fmean(item["value"] for item in by_label.values())),
    }


def _unique_case_score_pairs(
    rows: Sequence[dict[str, Any]], dimension: str
) -> tuple[list[tuple[float, float]], int]:
    case_values: dict[str, dict[str, set[float]]] = {}
    for row in rows:
        values = case_values.setdefault(
            row["case_id"], {"expert": set(), "system": set()}
        )
        expert_score = (row.get("expert_scores") or {}).get(dimension)
        system_score = (row.get("system_scores") or {}).get(dimension)
        if expert_score is not None:
            values["expert"].add(float(expert_score))
        if system_score is not None:
            values["system"].add(float(system_score))

    pairs: list[tuple[float, float]] = []
    unpaired = 0
    for case_id, values in case_values.items():
        if len(values["expert"]) > 1 or len(values["system"]) > 1:
            raise DatasetValidationError(
                f"inconsistent_duplicate_case_score:{case_id}:{dimension}"
            )
        if values["expert"] and values["system"]:
            pairs.append((next(iter(values["expert"])), next(iter(values["system"]))))
        elif values["expert"] or values["system"]:
            unpaired += 1
    return pairs, unpaired


def _dimension_mae(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for dimension in DIMENSIONS:
        pairs, unpaired = _unique_case_score_pairs(rows, dimension)
        if unpaired:
            result[dimension] = {
                "status": "NOT_VERIFIED_UNPAIRED_SCORES",
                "mae": None,
                "paired_case_count": len(pairs),
                "unpaired_case_count": unpaired,
            }
        elif pairs:
            result[dimension] = {
                "status": "VERIFIED",
                "mae": _round(fmean(abs(expert - system) for expert, system in pairs)),
                "paired_case_count": len(pairs),
                "unpaired_case_count": 0,
            }
        else:
            result[dimension] = {
                "status": "NOT_AVAILABLE",
                "mae": None,
                "paired_case_count": 0,
                "unpaired_case_count": 0,
            }
    return result


def _equivalence_disparity(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Compare system outcomes with equal weight per frozen equivalence group."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group = row.get("equivalence_group")
        if group is not None:
            grouped.setdefault(group, []).append(row)
    if not grouped:
        return {
            "status": "NOT_AVAILABLE",
            "group_count": 0,
            "pair_count": 0,
            "label_pair_count": 0,
            "outcome_disagreement_rate": None,
            "owner_disagreement_rate": None,
            "relation_disagreement_rate": None,
            "eligibility_disagreement_rate": None,
            "abstention_disagreement_rate": None,
            "exact_span_match_disagreement_rate": None,
            "present_axes": [],
            "missing_axes": sorted(EQUIVALENCE_AXES),
            "by_axis": {},
            "groups": {},
        }

    group_results: dict[str, dict[str, Any]] = {}
    for group, members in sorted(grouped.items()):
        axis = str(members[0]["equivalence_axis"])
        counts = {
            "pair_count": 0,
            "label_pair_count": 0,
            "outcome_disagreements": 0,
            "owner_disagreements": 0,
            "relation_disagreements": 0,
            "eligibility_disagreements": 0,
            "abstention_disagreements": 0,
            "exact_span_match_disagreements": 0,
        }
        for left, right in combinations(members, 2):
            left_abstain = bool(left["system"]["abstain"])
            right_abstain = bool(right["system"]["abstain"])
            left_outcome = (
                ("abstain",)
                if left_abstain
                else (
                    "prediction",
                    _system_exact_span_match(left),
                    *(left["system"][field] for field in LABEL_FIELDS),
                )
            )
            right_outcome = (
                ("abstain",)
                if right_abstain
                else (
                    "prediction",
                    _system_exact_span_match(right),
                    *(right["system"][field] for field in LABEL_FIELDS),
                )
            )
            counts["pair_count"] += 1
            counts["abstention_disagreements"] += int(
                left_abstain != right_abstain
            )
            counts["outcome_disagreements"] += int(left_outcome != right_outcome)
            if not left_abstain and not right_abstain:
                counts["label_pair_count"] += 1
                counts["owner_disagreements"] += int(
                    left["system"]["owner"] != right["system"]["owner"]
                )
                counts["relation_disagreements"] += int(
                    left["system"]["relation"] != right["system"]["relation"]
                )
                counts["eligibility_disagreements"] += int(
                    left["system"]["eligibility"]
                    != right["system"]["eligibility"]
                )
                counts["exact_span_match_disagreements"] += int(
                    _system_exact_span_match(left)
                    != _system_exact_span_match(right)
                )
        group_results[group] = {
            "axis": axis,
            "variants": sorted(str(row["style_variant"]) for row in members),
            **_render_equivalence_counts(counts),
        }

    by_axis: dict[str, dict[str, Any]] = {}
    for axis in sorted(EQUIVALENCE_AXES):
        axis_groups = [
            result for result in group_results.values() if result["axis"] == axis
        ]
        if axis_groups:
            by_axis[axis] = _macro_equivalence_groups(axis_groups)
    present_axes = sorted(by_axis)
    missing_axes = sorted(EQUIVALENCE_AXES - set(present_axes))
    return {
        "status": "VERIFIED" if not missing_axes else "PARTIAL_MISSING_AXES",
        "group_count": len(grouped),
        **_macro_equivalence_groups(list(group_results.values())),
        "present_axes": present_axes,
        "missing_axes": missing_axes,
        "by_axis": by_axis,
        "groups": group_results,
        "interpretation": (
            "Rates are macro-averaged across frozen content-equivalent groups; "
            "they do not establish demographic fairness or measurement invariance."
        ),
    }


def _render_equivalence_counts(counts: dict[str, int]) -> dict[str, Any]:
    pair_count = counts["pair_count"]
    label_pair_count = counts["label_pair_count"]
    return {
        "pair_count": pair_count,
        "label_pair_count": label_pair_count,
        "outcome_disagreement_rate": _round(counts["outcome_disagreements"] / pair_count),
        "abstention_disagreement_rate": _round(
            counts["abstention_disagreements"] / pair_count
        ),
        "owner_disagreement_rate": (
            _round(counts["owner_disagreements"] / label_pair_count)
            if label_pair_count
            else None
        ),
        "relation_disagreement_rate": (
            _round(counts["relation_disagreements"] / label_pair_count)
            if label_pair_count
            else None
        ),
        "eligibility_disagreement_rate": (
            _round(counts["eligibility_disagreements"] / label_pair_count)
            if label_pair_count
            else None
        ),
        "exact_span_match_disagreement_rate": (
            _round(counts["exact_span_match_disagreements"] / label_pair_count)
            if label_pair_count
            else None
        ),
    }


def _macro_equivalence_groups(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rate_fields = (
        "outcome_disagreement_rate",
        "abstention_disagreement_rate",
        "owner_disagreement_rate",
        "relation_disagreement_rate",
        "eligibility_disagreement_rate",
        "exact_span_match_disagreement_rate",
    )
    result: dict[str, Any] = {
        "group_count": len(groups),
        "pair_count": sum(int(group["pair_count"]) for group in groups),
        "label_pair_count": sum(int(group["label_pair_count"]) for group in groups),
        "aggregation": "macro_average_across_groups",
    }
    for field in rate_fields:
        values = [float(group[field]) for group in groups if group[field] is not None]
        result[field] = _round(fmean(values)) if values else None
        result[f"{field}_group_count"] = len(values)
    return result


def _empty_metrics() -> dict[str, Any]:
    return {
        "expert_agreement": None,
        "external_text_false_acceptance_rate": None,
        "unsafe_noneligible_auto_scoring_rate": None,
        "uncertain_auto_scoring_rate": None,
        "participant_reasoning_miss_rate": None,
        "exact_span_recovery_rate": None,
        "exact_span_match_rate_among_predictions": None,
        "macro_f1": None,
        "macro_f1_by_label": None,
        "abstention_rate": None,
        "dimension_mae": None,
        "content_equivalence_disparity": None,
    }


def _gate(passed: bool, **details: Any) -> dict[str, Any]:
    return {"passed": passed, **details}


def _coverage_gates(
    rows: Sequence[dict[str, Any]], metrics: dict[str, Any]
) -> dict[str, Any]:
    external_count = sum(
        row["adjudicated"]["owner"] in EXTERNAL_OWNERS for row in rows
    )
    participant_reasoning_count = sum(
        row["adjudicated"]["eligibility"] == "eligible" for row in rows
    )
    uncertain_count = sum(
        row["adjudicated"]["owner"] == "uncertain" for row in rows
    )
    noneligible_count = sum(
        row["adjudicated"]["eligibility"] != "eligible" for row in rows
    )

    label_support_by_field: dict[str, Any] = {}
    for field in LABEL_FIELDS:
        support = Counter(row["adjudicated"][field] for row in rows)
        missing_labels = [
            label for label in sorted(LABEL_SETS[field]) if support[label] == 0
        ]
        label_support_by_field[field] = {
            "passed": not missing_labels,
            "support": {
                label: support[label] for label in sorted(LABEL_SETS[field])
            },
            "missing_labels": missing_labels,
        }
    label_support_passed = all(
        item["passed"] for item in label_support_by_field.values()
    )

    equivalence = metrics["content_equivalence_disparity"]
    present_axes = set(equivalence.get("present_axes", []))
    missing_axes = sorted(EQUIVALENCE_AXES - present_axes)
    dimension_mae = metrics["dimension_mae"]
    verified_dimensions = [
        dimension
        for dimension in DIMENSIONS
        if dimension_mae[dimension]["status"] == "VERIFIED"
    ]
    missing_dimensions = [
        dimension for dimension in DIMENSIONS if dimension not in verified_dimensions
    ]
    agreement = metrics["expert_agreement"]
    undefined_kappa_fields = list(agreement["undefined_kappa_fields"])

    gates = {
        "annotations_complete": _gate(True, missing_annotation_count=0),
        "external_owner_coverage": _gate(
            external_count >= 1, row_count=external_count, minimum=1
        ),
        "participant_reasoning_coverage": _gate(
            participant_reasoning_count >= 1,
            row_count=participant_reasoning_count,
            minimum=1,
        ),
        "uncertain_owner_coverage": _gate(
            uncertain_count >= 1, row_count=uncertain_count, minimum=1
        ),
        "noneligible_coverage": _gate(
            noneligible_count >= 1, row_count=noneligible_count, minimum=1
        ),
        "contract_label_support": _gate(
            label_support_passed,
            by_field=label_support_by_field,
        ),
        "equivalence_axes_coverage": _gate(
            not missing_axes,
            required_axes=sorted(EQUIVALENCE_AXES),
            present_axes=sorted(present_axes),
            missing_axes=missing_axes,
        ),
        "six_dimension_paired_score_coverage": _gate(
            not missing_dimensions,
            verified_dimensions=verified_dimensions,
            missing_dimensions=missing_dimensions,
        ),
        "expert_kappa_defined": _gate(
            not undefined_kappa_fields,
            undefined_fields=undefined_kappa_fields,
        ),
    }
    return {"passed": all(item["passed"] for item in gates.values()), "gates": gates}


def _zero_safety_gate(rate: dict[str, Any]) -> dict[str, Any]:
    evaluated = bool(rate["denominator"])
    return {
        "passed": evaluated and rate["numerator"] == 0,
        "evaluated": evaluated,
        "numerator": rate["numerator"],
        "denominator": rate["denominator"],
        "required_value": 0,
    }


def _safety_gates(metrics: dict[str, Any]) -> dict[str, Any]:
    gates = {
        "external_auto_scoring_zero": _zero_safety_gate(
            metrics["external_text_false_acceptance_rate"]
        ),
        "uncertain_auto_scoring_zero": _zero_safety_gate(
            metrics["uncertain_auto_scoring_rate"]
        ),
        "all_noneligible_auto_scoring_zero": _zero_safety_gate(
            metrics["unsafe_noneligible_auto_scoring_rate"]
        ),
    }
    return {"passed": all(item["passed"] for item in gates.values()), "gates": gates}


def evaluate_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return strict metrics with separate completeness and coverage states."""

    if not rows:
        raise DatasetValidationError("gold_dataset_is_empty")
    missing = _validate_structure(rows)
    base: dict[str, Any] = {
        "protocol": PROTOCOL,
        "row_count": len(rows),
        "case_count": len({row["case_id"] for row in rows}),
        "annotation_completeness": {
            "complete_rows": len(rows)
            - len({item.split(":", 1)[0] for item in missing}),
            "incomplete_rows": len({item.split(":", 1)[0] for item in missing}),
            "missing_annotation_count": len(missing),
            "missing_annotations": missing,
        },
    }
    if missing:
        return {
            **base,
            "status": "NOT_VERIFIED",
            "reason": "expert_or_system_annotations_incomplete",
            "acceptance_gates": {
                "coverage_passed": False,
                "safety_passed": None,
                "overall_passed": False,
                "coverage": {
                    "passed": False,
                    "gates": {
                        "annotations_complete": _gate(
                            False,
                            missing_annotation_count=len(missing),
                        )
                    },
                },
                "safety": {"passed": None, "status": "NOT_EVALUATED"},
            },
            "metrics": _empty_metrics(),
        }

    external_rows = [
        row for row in rows if row["adjudicated"]["owner"] in EXTERNAL_OWNERS
    ]
    external_false_acceptances = sum(
        row["system"]["used_for_scoring"] for row in external_rows
    )
    uncertain_rows = [
        row for row in rows if row["adjudicated"]["owner"] == "uncertain"
    ]
    uncertain_auto_scoring = sum(
        row["system"]["used_for_scoring"] for row in uncertain_rows
    )
    noneligible_rows = [
        row for row in rows if row["adjudicated"]["eligibility"] != "eligible"
    ]
    unsafe_noneligible_auto_scoring = sum(
        row["system"]["used_for_scoring"] for row in noneligible_rows
    )
    participant_reasoning_rows = [
        row for row in rows if row["adjudicated"]["eligibility"] == "eligible"
    ]
    participant_reasoning_misses = sum(
        row["system"]["abstain"]
        or not _system_exact_span_match(row)
        or row["system"]["eligibility"] != "eligible"
        for row in participant_reasoning_rows
    )
    abstentions = sum(row["system"]["abstain"] for row in rows)
    predictions = [row for row in rows if not row["system"]["abstain"]]
    exact_span_matches = sum(_system_exact_span_match(row) for row in rows)
    exact_predicted_span_matches = sum(
        _system_exact_span_match(row) for row in predictions
    )
    classification = _classification_metrics(rows)
    expert_agreement = _expert_agreement(rows)
    dimension_mae = _dimension_mae(rows)
    equivalence = _equivalence_disparity(rows)
    metrics = {
        "expert_agreement": expert_agreement,
        "external_text_false_acceptance_rate": _rate(
            external_false_acceptances, len(external_rows)
        ),
        "unsafe_noneligible_auto_scoring_rate": _rate(
            unsafe_noneligible_auto_scoring, len(noneligible_rows)
        ),
        "uncertain_auto_scoring_rate": _rate(
            uncertain_auto_scoring, len(uncertain_rows)
        ),
        "participant_reasoning_miss_rate": _rate(
            participant_reasoning_misses, len(participant_reasoning_rows)
        ),
        "exact_span_recovery_rate": _rate(exact_span_matches, len(rows)),
        "exact_span_match_rate_among_predictions": _rate(
            exact_predicted_span_matches, len(predictions)
        ),
        "macro_f1": classification["macro_f1"],
        "macro_f1_by_label": classification["by_label"],
        "abstention_rate": _rate(abstentions, len(rows)),
        "dimension_mae": dimension_mae,
        "content_equivalence_disparity": equivalence,
    }
    coverage = _coverage_gates(rows, metrics)
    safety = _safety_gates(metrics)
    status = "VERIFIED" if coverage["passed"] else "PARTIAL"

    return {
        **base,
        "status": status,
        "reason": None if status == "VERIFIED" else "acceptance_coverage_incomplete",
        "acceptance_gates": {
            "coverage_passed": coverage["passed"],
            "safety_passed": safety["passed"],
            "overall_passed": coverage["passed"] and safety["passed"],
            "coverage": coverage,
            "safety": safety,
        },
        "metrics": metrics,
    }


def evaluate_path(path: Path) -> dict[str, Any]:
    return evaluate_rows(load_jsonl(path))


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a fully adjudicated evidence-attribution gold JSONL file."
    )
    parser.add_argument("input", type=Path, help="Gold JSONL path")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path; stdout is always emitted",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = evaluate_path(args.input)
    except DatasetValidationError as exc:
        print(
            json.dumps(
                {"protocol": PROTOCOL, "status": "INVALID", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"cannot_write_output:{args.output}:{exc}", file=sys.stderr)
            return 1
    return 0 if result["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
