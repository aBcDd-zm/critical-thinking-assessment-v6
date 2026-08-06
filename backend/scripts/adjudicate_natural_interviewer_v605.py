#!/usr/bin/env python3
"""Validate blinded reviews and enforce the v6.0.5 promotion stop gate."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from scripts import compare_natural_interviewer_prompts_v604 as base
from scripts import compare_natural_interviewer_prompts_v605 as runner


NON_INFERIORITY_DIMENSIONS = (
    "empathy_accuracy_and_evidence",
    "warmth",
    "human_likeness",
    "willingness_to_continue",
)


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"json_root_must_be_object:{path.name}")
    return value


def _expected_candidates(packet: dict[str, Any]) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {}
    cases = packet.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit("blind_packet_cases_invalid")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise SystemExit("blind_packet_case_invalid")
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise SystemExit("blind_packet_candidate_count_invalid")
        candidate_ids = {
            candidate.get("candidate_id")
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
        }
        if len(candidate_ids) != 2:
            raise SystemExit("blind_packet_candidate_ids_invalid")
        expected[case["case_id"]] = candidate_ids
    return expected


def _validate_review(
    review: dict[str, Any],
    *,
    packet_sha256: str,
    expected: dict[str, set[str]],
) -> str:
    reviewer_id = review.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise SystemExit("reviewer_id_required")
    if review.get("protocol") != runner.RUNNER_PROTOCOL:
        raise SystemExit(f"review_protocol_mismatch:{reviewer_id}")
    if review.get("blind_packet_sha256") != packet_sha256:
        raise SystemExit(f"blind_packet_sha_mismatch:{reviewer_id}")
    cases = review.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected):
        raise SystemExit(f"review_case_coverage_invalid:{reviewer_id}")

    seen_cases: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("case_id") not in expected:
            raise SystemExit(f"review_case_id_invalid:{reviewer_id}")
        case_id = case["case_id"]
        if case_id in seen_cases:
            raise SystemExit(f"review_case_duplicate:{reviewer_id}")
        seen_cases.add(case_id)
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise SystemExit(f"review_candidates_invalid:{reviewer_id}:{case_id}")
        seen_candidates: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("candidate_id") not in expected[case_id]:
                raise SystemExit(f"review_candidate_id_invalid:{reviewer_id}:{case_id}")
            candidate_id = candidate["candidate_id"]
            if candidate_id in seen_candidates:
                raise SystemExit(f"review_candidate_duplicate:{reviewer_id}:{case_id}")
            seen_candidates.add(candidate_id)
            ratings = candidate.get("ratings")
            if not isinstance(ratings, dict) or set(ratings) != set(runner.REVIEW_DIMENSIONS):
                raise SystemExit(f"review_rating_dimensions_invalid:{reviewer_id}:{case_id}")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5
                for value in ratings.values()
            ):
                raise SystemExit(f"review_rating_value_invalid:{reviewer_id}:{case_id}")
            severe_codes = candidate.get("severe_regression_codes")
            if not isinstance(severe_codes, list) or any(
                code not in runner.SEVERE_REGRESSION_CODES for code in severe_codes
            ):
                raise SystemExit(f"review_severe_code_invalid:{reviewer_id}:{case_id}")
            if len(severe_codes) != len(set(severe_codes)):
                raise SystemExit(f"review_severe_code_duplicate:{reviewer_id}:{case_id}")
            if severe_codes and not str(candidate.get("reviewer_notes", "")).strip():
                raise SystemExit(f"severe_code_requires_note:{reviewer_id}:{case_id}")

        tie = case.get("tie")
        preferred = case.get("preferred_candidate_id")
        if not isinstance(tie, bool):
            raise SystemExit(f"review_tie_invalid:{reviewer_id}:{case_id}")
        if tie:
            if preferred is not None:
                raise SystemExit(f"tie_must_not_have_preference:{reviewer_id}:{case_id}")
        elif preferred not in expected[case_id]:
            raise SystemExit(f"preference_required:{reviewer_id}:{case_id}")
    return reviewer_id.strip()


def adjudicate(run_dir: Path, review_paths: list[Path]) -> dict[str, Any]:
    if len(review_paths) != 2:
        raise SystemExit("exactly_two_blind_reviews_required")
    packet_path = run_dir / "blind_review_packet.json"
    manifest_path = run_dir / "run_manifest.json"
    mapping_path = run_dir / "sealed" / "opaque_mapping.json"
    output_path = run_dir / "sealed" / "v605_blind_adjudication.json"
    if output_path.exists():
        raise SystemExit("adjudication_output_must_not_exist")

    packet = _load_object(packet_path)
    manifest = _load_object(manifest_path)
    mapping = _load_object(mapping_path)
    if manifest.get("status") != "completed":
        raise SystemExit("generation_run_not_completed")
    if packet.get("protocol") != runner.RUNNER_PROTOCOL:
        raise SystemExit("blind_packet_protocol_mismatch")
    packet_sha256 = base._sha256_file(packet_path)
    expected = _expected_candidates(packet)
    reviews = [_load_object(path) for path in review_paths]
    reviewer_ids = [
        _validate_review(
            review,
            packet_sha256=packet_sha256,
            expected=expected,
        )
        for review in reviews
    ]
    if len(set(reviewer_ids)) != 2:
        raise SystemExit("reviewer_ids_must_be_distinct")

    arm_key: dict[tuple[str, str], str] = {}
    for item in mapping.get("candidate_arm_key", []):
        if not isinstance(item, dict):
            raise SystemExit("sealed_mapping_invalid")
        key = (item.get("case_id"), item.get("candidate_id"))
        version = item.get("prompt_version")
        if key[0] not in expected or key[1] not in expected[key[0]] or version not in runner.PROMPT_VERSIONS:
            raise SystemExit("sealed_mapping_entry_invalid")
        if key in arm_key:
            raise SystemExit("sealed_mapping_duplicate")
        arm_key[key] = version
    if len(arm_key) != sum(len(value) for value in expected.values()):
        raise SystemExit("sealed_mapping_coverage_invalid")

    ratings_by_version: dict[str, dict[str, list[int]]] = {
        version: {dimension: [] for dimension in runner.REVIEW_DIMENSIONS}
        for version in runner.PROMPT_VERSIONS
    }
    severe_cases: dict[str, dict[str, set[str]]] = {
        version: {code: set() for code in runner.SEVERE_REGRESSION_CODES}
        for version in runner.PROMPT_VERSIONS
    }
    preference_votes = {version: 0 for version in runner.PROMPT_VERSIONS}
    tie_votes = 0

    for review in reviews:
        for case in review["cases"]:
            case_id = case["case_id"]
            if case["tie"]:
                tie_votes += 1
            else:
                preference_votes[arm_key[(case_id, case["preferred_candidate_id"])]] += 1
            for candidate in case["candidates"]:
                version = arm_key[(case_id, candidate["candidate_id"])]
                for dimension, value in candidate["ratings"].items():
                    ratings_by_version[version][dimension].append(value)
                for code in candidate["severe_regression_codes"]:
                    severe_cases[version][code].add(case_id)

    means = {
        version: {
            dimension: round(fmean(values), 4)
            for dimension, values in dimensions.items()
        }
        for version, dimensions in ratings_by_version.items()
    }
    severe_case_counts = {
        version: {
            code: len(case_ids)
            for code, case_ids in codes.items()
        }
        for version, codes in severe_cases.items()
    }
    v605_stop_codes = [
        code
        for code, count in severe_case_counts["v6.0.5"].items()
        if count >= 2
    ]
    noninferiority = {
        dimension: means["v6.0.5"][dimension] >= means["v6.0.4"][dimension]
        for dimension in NON_INFERIORITY_DIMENSIONS
    }
    eligible = not v605_stop_codes and all(noninferiority.values())
    result = {
        "protocol": runner.RUNNER_PROTOCOL,
        "blind_packet_sha256": packet_sha256,
        "reviewer_ids": reviewer_ids,
        "severe_gate_rule": (
            "STOP when the same severe code is assigned to v6.0.5 in at least "
            "two distinct case_ids; repeated rounds or reviewers within one case count once."
        ),
        "severe_case_counts_by_version": severe_case_counts,
        "v605_stop_codes": v605_stop_codes,
        "noninferiority_dimensions": list(NON_INFERIORITY_DIMENSIONS),
        "noninferiority_by_dimension": noninferiority,
        "mean_ratings_by_version": means,
        "preference_votes_by_version": preference_votes,
        "tie_votes": tie_votes,
        "v605_eligible_for_default": eligible,
        "decision": "PROMOTE_V605" if eligible else "KEEP_V604",
        "automated_model_judge_used": False,
    }
    base._write_private_json(output_path, result)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--review", required=True, action="append", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = adjudicate(args.run_dir.resolve(), [path.resolve() for path in args.review])
    print(
        json.dumps(
            {
                "decision": result["decision"],
                "v605_eligible_for_default": result["v605_eligible_for_default"],
                "v605_stop_codes": result["v605_stop_codes"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
