#!/usr/bin/env python3
"""Validate two blind reviews and apply the v6.2.3 qualitative release gate."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean, quantiles
from typing import Any

from scripts import compare_natural_interviewer_prompts_v604 as private_io
from scripts import compare_natural_interviewer_prompts_v623 as runner


PRIMARY_IMPROVEMENT_DIMENSIONS = (
    "warmth_and_emotional_attunement",
    "naturalness_and_human_likeness",
    "non_interrogative_tone",
    "willingness_to_continue",
)
CORE_NONINFERIORITY_DIMENSIONS = (
    "evidence_grounded_tentativeness",
    "concrete_event_anchoring",
    "information_gain_and_mainline_continuity",
    "repair_and_boundary_respect",
    "single_clear_question_and_safety",
)
PRIMARY_COMPOSITE_MINIMUM_GAIN = 0.25
PER_DIMENSION_MEANINGFUL_GAIN = 0.25
CORE_NONINFERIORITY_MARGIN = 0.25
REPAIR_RATE_MARGIN = 0.05
MAX_V623_REPAIR_RATE = 0.10
MEAN_ATTEMPT_MARGIN = 0.10
MAX_V623_MEAN_ATTEMPTS = 1.20
P95_LATENCY_RATIO = 1.35
P95_LATENCY_MARGIN_MS = 1_000.0
MAX_V623_P95_LATENCY_MS = 20_000.0
MAX_REASONABLE_LOGICAL_LATENCY_MS = 85_000
IMMEDIATE_STOP_CODES = frozenset(
    {
        "severe_overclaim_or_diagnosis",
        "severe_structure_or_no_question_failure",
        "severe_mainline_or_finish_regression",
    }
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
    cases = packet.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit("blind_packet_cases_invalid")
    expected: dict[str, set[str]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise SystemExit("blind_packet_case_invalid")
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise SystemExit("blind_packet_candidate_count_invalid")
        candidate_ids = {
            candidate.get("candidate_id")
            for candidate in candidates
            if isinstance(candidate, dict)
            and isinstance(candidate.get("candidate_id"), str)
        }
        if len(candidate_ids) != 2:
            raise SystemExit("blind_packet_candidate_ids_invalid")
        if case["case_id"] in expected:
            raise SystemExit("blind_packet_case_duplicate")
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
    reviewer_id = reviewer_id.strip()
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
        case_id = str(case["case_id"])
        if case_id in seen_cases:
            raise SystemExit(f"review_case_duplicate:{reviewer_id}")
        seen_cases.add(case_id)
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise SystemExit(f"review_candidates_invalid:{reviewer_id}:{case_id}")
        seen_candidates: set[str] = set()
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or candidate.get("candidate_id") not in expected[case_id]
            ):
                raise SystemExit(
                    f"review_candidate_id_invalid:{reviewer_id}:{case_id}"
                )
            candidate_id = str(candidate["candidate_id"])
            if candidate_id in seen_candidates:
                raise SystemExit(
                    f"review_candidate_duplicate:{reviewer_id}:{case_id}"
                )
            seen_candidates.add(candidate_id)
            ratings = candidate.get("ratings")
            if not isinstance(ratings, dict) or set(ratings) != set(
                runner.REVIEW_DIMENSIONS
            ):
                raise SystemExit(
                    f"review_rating_dimensions_invalid:{reviewer_id}:{case_id}"
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 5
                for value in ratings.values()
            ):
                raise SystemExit(
                    f"review_rating_value_invalid:{reviewer_id}:{case_id}"
                )
            severe_codes = candidate.get("severe_regression_codes")
            if not isinstance(severe_codes, list) or any(
                code not in runner.SEVERE_REGRESSION_CODES for code in severe_codes
            ):
                raise SystemExit(
                    f"review_severe_code_invalid:{reviewer_id}:{case_id}"
                )
            if len(severe_codes) != len(set(severe_codes)):
                raise SystemExit(
                    f"review_severe_code_duplicate:{reviewer_id}:{case_id}"
                )
            problematic = candidate.get("problematic_round_indices")
            if not isinstance(problematic, list) or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= runner.ROUNDS_PER_ARM
                for value in problematic
            ):
                raise SystemExit(
                    f"review_problematic_round_invalid:{reviewer_id}:{case_id}"
                )
            if severe_codes and (
                not problematic
                or not str(candidate.get("reviewer_notes", "")).strip()
            ):
                raise SystemExit(
                    f"severe_code_requires_round_and_note:{reviewer_id}:{case_id}"
                )

        tie = case.get("tie")
        preferred = case.get("preferred_candidate_id")
        if not isinstance(tie, bool):
            raise SystemExit(f"review_tie_invalid:{reviewer_id}:{case_id}")
        if tie:
            if preferred is not None:
                raise SystemExit(
                    f"tie_must_not_have_preference:{reviewer_id}:{case_id}"
                )
        elif preferred not in expected[case_id]:
            raise SystemExit(f"preference_required:{reviewer_id}:{case_id}")
        if not str(case.get("preference_reason", "")).strip():
            raise SystemExit(f"preference_reason_required:{reviewer_id}:{case_id}")
    return reviewer_id


def _validated_mapping(
    mapping: dict[str, Any],
    expected: dict[str, set[str]],
) -> tuple[dict[tuple[str, str], str], dict[str, str], dict[str, int]]:
    scenario_by_case: dict[str, str] = {}
    replicate_by_case: dict[str, int] = {}
    replicates_by_scenario: dict[str, set[int]] = defaultdict(set)
    scenario_catalog = {item.scenario_type: item for item in runner.SCENARIOS}
    allowed_scenarios = set(scenario_catalog)
    for item in mapping.get("case_key", []):
        if not isinstance(item, dict):
            raise SystemExit("sealed_case_mapping_invalid")
        case_id = item.get("case_id")
        scenario_type = item.get("scenario_type")
        replicate_index = item.get("replicate_index")
        if (
            case_id not in expected
            or scenario_type not in allowed_scenarios
            or isinstance(replicate_index, bool)
            or not isinstance(replicate_index, int)
            or not 1 <= replicate_index <= runner.REPLICATES_PER_SCENARIO
        ):
            raise SystemExit("sealed_case_mapping_entry_invalid")
        scenario = scenario_catalog[str(scenario_type)]
        expected_scenario_sha256 = private_io._sha256_text(
            private_io._canonical_json(
                {
                    "user_turns": list(scenario.user_turns),
                    "round_policies": list(scenario.round_policies),
                }
            )
        )
        if item.get("scenario_sha256") != expected_scenario_sha256:
            raise SystemExit("sealed_case_scenario_hash_mismatch")
        if case_id in scenario_by_case:
            raise SystemExit("sealed_case_mapping_duplicate")
        scenario_by_case[case_id] = scenario_type
        replicate_by_case[case_id] = replicate_index
        if replicate_index in replicates_by_scenario[scenario_type]:
            raise SystemExit("sealed_case_replicate_duplicate")
        replicates_by_scenario[scenario_type].add(replicate_index)
    if set(scenario_by_case) != set(expected):
        raise SystemExit("sealed_case_mapping_coverage_invalid")
    expected_replicates = set(range(1, runner.REPLICATES_PER_SCENARIO + 1))
    if set(replicates_by_scenario) != allowed_scenarios or any(
        values != expected_replicates for values in replicates_by_scenario.values()
    ):
        raise SystemExit("sealed_case_replicate_coverage_invalid")

    arm_key: dict[tuple[str, str], str] = {}
    prompt_assets = runner.prompt_assets_payload()
    for item in mapping.get("candidate_arm_key", []):
        if not isinstance(item, dict):
            raise SystemExit("sealed_arm_mapping_invalid")
        key = (item.get("case_id"), item.get("candidate_id"))
        version = item.get("prompt_version")
        if (
            key[0] not in expected
            or key[1] not in expected[key[0]]
            or version not in runner.PROMPT_VERSIONS
        ):
            raise SystemExit("sealed_arm_mapping_entry_invalid")
        expected_prompt = prompt_assets[str(version)]
        if (
            item.get("prompt_id") != expected_prompt["prompt_id"]
            or item.get("prompt_sha256") != expected_prompt["prompt_sha256"]
        ):
            raise SystemExit("sealed_arm_prompt_asset_mismatch")
        if key in arm_key:
            raise SystemExit("sealed_arm_mapping_duplicate")
        arm_key[(str(key[0]), str(key[1]))] = str(version)
    if len(arm_key) != sum(len(candidates) for candidates in expected.values()):
        raise SystemExit("sealed_arm_mapping_coverage_invalid")
    for case_id, candidate_ids in expected.items():
        if {
            arm_key[(case_id, candidate_id)] for candidate_id in candidate_ids
        } != set(runner.PROMPT_VERSIONS):
            raise SystemExit("sealed_arm_pair_invalid")
    return arm_key, scenario_by_case, replicate_by_case


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_generation_provenance(manifest: dict[str, Any]) -> None:
    if manifest.get("source_sha256") != runner._source_hashes():
        raise SystemExit("generation_source_hash_mismatch")
    if manifest.get("prompt_assets") != runner.prompt_assets_payload():
        raise SystemExit("generation_prompt_assets_mismatch")
    expected_scenario_hash = private_io._sha256_text(
        private_io._canonical_json(runner.scenario_plan_payload())
    )
    if manifest.get("scenario_plan_sha256") != expected_scenario_hash:
        raise SystemExit("generation_scenario_plan_hash_mismatch")
    try:
        endpoint = runner.official_deepseek_endpoint(
            runner.settings.deepseek_base_url
        )
    except BaseException as exc:
        raise SystemExit("generation_official_endpoint_unavailable") from exc
    expected_endpoint_fields = {
        "configured_deepseek_base_url": endpoint["configured_base_url"],
        "provider_completion_url": endpoint["completion_url"],
        "provider_completion_url_sha256": endpoint[
            "completion_url_sha256"
        ],
        "provider_host": endpoint["host"],
        "provider_host_sha256": endpoint["host_sha256"],
    }
    if any(
        manifest.get(key) != value
        for key, value in expected_endpoint_fields.items()
    ):
        raise SystemExit("generation_provider_endpoint_mismatch")


def _load_and_verify_events(
    *,
    events_path: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    if not events_path.is_file():
        raise SystemExit("generation_events_jsonl_missing")
    expected_hash = manifest.get("events_jsonl_sha256")
    if not _is_sha256(expected_hash):
        raise SystemExit("generation_events_hash_invalid")
    if private_io._sha256_file(events_path) != expected_hash:
        raise SystemExit("generation_events_hash_mismatch")
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit("generation_events_jsonl_unreadable") from exc
    expected_count = manifest.get("events_jsonl_event_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count <= 0
        or len(lines) != expected_count
    ):
        raise SystemExit("generation_events_count_mismatch")
    events: list[dict[str, Any]] = []
    previous_recorded_at: int | None = None
    for line_index, line in enumerate(lines, start=1):
        if not line.strip():
            raise SystemExit("generation_events_blank_line")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit("generation_events_invalid_json") from exc
        if not isinstance(event, dict):
            raise SystemExit("generation_event_not_object")
        recorded_at = event.get("recorded_at_unix_ms")
        if (
            isinstance(recorded_at, bool)
            or not isinstance(recorded_at, int)
            or recorded_at < 0
            or (
                previous_recorded_at is not None
                and recorded_at < previous_recorded_at
            )
        ):
            raise SystemExit(
                f"generation_event_timestamp_invalid:{line_index}"
            )
        previous_recorded_at = recorded_at
        events.append(event)
    return events


def _logical_event_context(
    *,
    record: dict[str, Any],
    scenario_by_case: dict[str, str],
    replicate_by_case: dict[str, int],
) -> dict[str, Any]:
    case_id = str(record["case_id"])
    return {
        "scenario_type": scenario_by_case[case_id],
        "replicate_index": replicate_by_case[case_id],
        "prompt_version": record["prompt_version"],
        "round_index": record["round_index"],
        "case_id": case_id,
        "candidate_id": record["candidate_id"],
    }


def _require_event_fields(
    event: dict[str, Any],
    expected: dict[str, Any],
    error: str,
) -> None:
    if any(event.get(key) != value for key, value in expected.items()):
        raise SystemExit(error)


def _replay_completed_event_chain(
    *,
    events: list[dict[str, Any]],
    manifest: dict[str, Any],
    scenario_by_case: dict[str, str],
    replicate_by_case: dict[str, int],
) -> None:
    records = manifest["logical_call_records"]
    event_cursor = 0
    next_physical_index = 1
    observed_repairs = 0
    expected_completion_url = str(manifest["provider_completion_url"])
    expected_completion_url_sha256 = str(
        manifest["provider_completion_url_sha256"]
    )
    expected_host = str(manifest["provider_host"])
    expected_host_sha256 = str(manifest["provider_host_sha256"])

    def consume(expected_event: str) -> dict[str, Any]:
        nonlocal event_cursor
        if event_cursor >= len(events):
            raise SystemExit(f"generation_events_truncated:{expected_event}")
        event = events[event_cursor]
        event_cursor += 1
        if event.get("event") != expected_event:
            if event.get("event") == "physical_call_failed":
                raise SystemExit("generation_physical_failure_in_completed_run")
            if event.get("event") == "logical_call_failed":
                raise SystemExit("generation_logical_failure_in_completed_run")
            raise SystemExit(f"generation_event_order_invalid:{expected_event}")
        return event

    for record in records:
        context = _logical_event_context(
            record=record,
            scenario_by_case=scenario_by_case,
            replicate_by_case=replicate_by_case,
        )
        logical_started = consume("logical_call_started")
        _require_event_fields(
            logical_started,
            context,
            "generation_logical_start_context_mismatch",
        )

        attempt_count = int(record["attempt_count"])
        if bool(record["repair_used"]) != (attempt_count > 1):
            raise SystemExit("generation_repair_attempt_relationship_invalid")
        if int(record["physical_requests_for_logical_call"]) != attempt_count:
            raise SystemExit("generation_event_attempt_count_mismatch")
        raw_content_hashes: list[str] = []
        for _ in range(attempt_count):
            physical_started = consume("physical_call_started")
            if (
                physical_started.get("physical_request_index")
                != next_physical_index
                or physical_started.get("physical_request_cap")
                != runner.MAX_PHYSICAL_REQUESTS
                or physical_started.get("request_url")
                != expected_completion_url
                or physical_started.get("request_url_sha256")
                != expected_completion_url_sha256
                or physical_started.get("request_host") != expected_host
                or physical_started.get("request_host_sha256")
                != expected_host_sha256
                or not _is_sha256(
                    physical_started.get("request_payload_sha256")
                )
                or physical_started.get("requested_model")
                != runner.EXPECTED_MODEL
                or physical_started.get("requested_thinking")
                != {"type": runner.EXPECTED_THINKING}
                or physical_started.get("requested_max_tokens")
                != runner.EXPECTED_MAX_TOKENS
            ):
                raise SystemExit("generation_physical_start_invalid")
            physical_completed = consume("physical_call_completed")
            raw_content_sha256 = physical_completed.get("raw_content_sha256")
            if (
                physical_completed.get("physical_request_index")
                != next_physical_index
                or not isinstance(
                    physical_completed.get("http_status_code"), int
                )
                or not 200
                <= int(physical_completed["http_status_code"])
                < 300
                or physical_completed.get("api_raw_model")
                != runner.EXPECTED_MODEL
                or not _is_sha256(raw_content_sha256)
            ):
                raise SystemExit("generation_physical_completion_invalid")
            raw_content_hashes.append(str(raw_content_sha256))
            next_physical_index += 1

        if record.get("provider") != runner.EXPECTED_PROVIDER:
            raise SystemExit("generation_provider_mismatch")
        if record.get("model") != runner.EXPECTED_MODEL:
            raise SystemExit("generation_record_model_mismatch")
        if record.get("api_raw_model") != runner.EXPECTED_MODEL:
            raise SystemExit("generation_record_raw_model_mismatch")
        if (
            record.get("provider_request_url") != expected_completion_url
            or record.get("provider_request_url_sha256")
            != expected_completion_url_sha256
            or record.get("provider_request_host") != expected_host
            or record.get("provider_request_host_sha256")
            != expected_host_sha256
        ):
            raise SystemExit("generation_record_provider_endpoint_mismatch")
        if record.get("physical_response_raw_content_sha256") != raw_content_hashes:
            raise SystemExit("generation_record_raw_response_hash_mismatch")

        logical_completed = consume("logical_call_completed")
        _require_event_fields(
            logical_completed,
            {
                **context,
                "output_text_sha256": record["output_text_sha256"],
                "session_action": record["session_action"],
                "finish_reason": record["finish_reason"],
                "provider": record["provider"],
                "model": record["model"],
                "api_raw_model": record["api_raw_model"],
                "provider_request_url": record["provider_request_url"],
                "provider_request_url_sha256": record[
                    "provider_request_url_sha256"
                ],
                "provider_request_host": record["provider_request_host"],
                "provider_request_host_sha256": record[
                    "provider_request_host_sha256"
                ],
                "physical_response_raw_content_sha256": raw_content_hashes,
                "repair_used": record["repair_used"],
                "attempt_count": record["attempt_count"],
                "latency_ms": record["latency_ms"],
                "physical_requests_for_logical_call": record[
                    "physical_requests_for_logical_call"
                ],
            },
            "generation_logical_completion_mismatch",
        )
        observed_repairs += int(bool(record["repair_used"]))

    if event_cursor != len(events):
        trailing_event = events[event_cursor].get("event")
        raise SystemExit(f"generation_events_trailing:{trailing_event}")
    physical_observed = next_physical_index - 1
    if (
        physical_observed != manifest.get("physical_requests_observed")
        or physical_observed > runner.MAX_PHYSICAL_REQUESTS
    ):
        raise SystemExit("generation_event_physical_total_mismatch")
    if observed_repairs != manifest.get("repair_calls_observed"):
        raise SystemExit("generation_event_repair_total_mismatch")


def _validate_logical_records(
    *,
    manifest: dict[str, Any],
    packet: dict[str, Any],
    arm_key: dict[tuple[str, str], str],
    scenario_by_case: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    records = manifest["logical_call_records"]
    packet_rounds: dict[tuple[str, str, int], dict[str, Any]] = {}
    scenario_catalog = {item.scenario_type: item for item in runner.SCENARIOS}

    for case in packet["cases"]:
        case_id = str(case["case_id"])
        scenario = scenario_catalog[scenario_by_case[case_id]]
        for candidate in case["candidates"]:
            candidate_id = str(candidate["candidate_id"])
            if candidate.get("opening") != runner.CANONICAL_OPENING:
                raise SystemExit("blind_packet_opening_mismatch")
            rounds = candidate["rounds"]
            seen_rounds: set[int] = set()
            transcript: list[dict[str, Any]] = [
                {
                    "turn_index": 0,
                    "role": "assistant",
                    "content": runner.CANONICAL_OPENING,
                }
            ]
            for round_payload in rounds:
                if not isinstance(round_payload, dict):
                    raise SystemExit("blind_packet_round_invalid")
                round_index = round_payload.get("round_index")
                if (
                    isinstance(round_index, bool)
                    or not isinstance(round_index, int)
                    or not 1 <= round_index <= runner.ROUNDS_PER_ARM
                    or round_index in seen_rounds
                ):
                    raise SystemExit("blind_packet_round_index_invalid")
                seen_rounds.add(round_index)
                user_text = round_payload.get("user_text")
                assistant_text = round_payload.get("assistant_text")
                action = round_payload.get("session_action")
                reason = round_payload.get("finish_reason")
                ended = round_payload.get("conversation_ended_after_this_turn")
                if user_text != scenario.user_turns[round_index - 1]:
                    raise SystemExit("blind_packet_scenario_text_mismatch")
                if not isinstance(assistant_text, str) or not assistant_text.strip():
                    raise SystemExit("blind_packet_assistant_text_invalid")
                if action not in {"continue", "finish"}:
                    raise SystemExit("blind_packet_session_action_invalid")
                if (action == "continue" and reason is not None) or (
                    action == "finish" and reason != "user_requested"
                ):
                    raise SystemExit("blind_packet_finish_reason_invalid")
                if not isinstance(ended, bool) or ended is not (action == "finish"):
                    raise SystemExit("blind_packet_ended_flag_mismatch")
                expected_policy = scenario.round_policies[round_index - 1]
                if (expected_policy == "finish") is not (action == "finish"):
                    raise SystemExit("blind_packet_action_policy_mismatch")
                key = (case_id, candidate_id, round_index)
                if key in packet_rounds:
                    raise SystemExit("blind_packet_round_duplicate")
                transcript.append(
                    {
                        "turn_index": len(transcript),
                        "role": "user",
                        "content": user_text,
                    }
                )
                expected_payload = runner.build_production_payload(transcript)
                packet_rounds[key] = {
                    **round_payload,
                    "expected_input_payload_sha256": private_io._sha256_text(
                        private_io._canonical_json(expected_payload)
                    ),
                    "transcript_before_response": list(transcript),
                    "scenario": scenario,
                }
                transcript.append(
                    {
                        "turn_index": len(transcript),
                        "role": "assistant",
                        "content": assistant_text,
                    }
                )
            if seen_rounds != set(range(1, runner.ROUNDS_PER_ARM + 1)):
                raise SystemExit("blind_packet_round_coverage_invalid")

    seen_records: set[tuple[str, str, int]] = set()
    records_by_version: dict[str, list[dict[str, Any]]] = {
        version: [] for version in runner.PROMPT_VERSIONS
    }
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("generation_logical_record_invalid")
        case_id = record.get("case_id")
        candidate_id = record.get("candidate_id")
        round_index = record.get("round_index")
        if (
            not isinstance(case_id, str)
            or not isinstance(candidate_id, str)
            or isinstance(round_index, bool)
            or not isinstance(round_index, int)
        ):
            raise SystemExit("generation_logical_record_key_invalid")
        key = (case_id, candidate_id, round_index)
        if key not in packet_rounds or key in seen_records:
            raise SystemExit("generation_logical_record_coverage_invalid")
        seen_records.add(key)
        version = arm_key.get((case_id, candidate_id))
        if record.get("prompt_version") != version:
            raise SystemExit("generation_logical_record_version_mismatch")
        packet_round = packet_rounds[key]
        if record.get("input_payload_sha256") != packet_round[
            "expected_input_payload_sha256"
        ]:
            raise SystemExit("generation_input_payload_hash_mismatch")
        output_hash = record.get("output_text_sha256")
        if not _is_sha256(output_hash) or output_hash != private_io._sha256_text(
            str(packet_round["assistant_text"])
        ):
            raise SystemExit("generation_output_text_hash_mismatch")
        if record.get("session_action") != packet_round["session_action"]:
            raise SystemExit("generation_session_action_mismatch")
        if record.get("finish_reason") != packet_round["finish_reason"]:
            raise SystemExit("generation_finish_reason_mismatch")

        repair_used = record.get("repair_used")
        attempt_count = record.get("attempt_count")
        latency_ms = record.get("latency_ms")
        physical_requests = record.get("physical_requests_for_logical_call")
        if not isinstance(repair_used, bool):
            raise SystemExit("generation_repair_flag_invalid")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or not 1 <= attempt_count <= 3
        ):
            raise SystemExit("generation_attempt_count_invalid")
        if (
            isinstance(physical_requests, bool)
            or not isinstance(physical_requests, int)
            or physical_requests != attempt_count
        ):
            raise SystemExit("generation_physical_attempt_mismatch")
        if (
            isinstance(latency_ms, bool)
            or not isinstance(latency_ms, int)
            or not 0 <= latency_ms <= MAX_REASONABLE_LOGICAL_LATENCY_MS
        ):
            raise SystemExit("generation_latency_invalid")
        flags = record.get("audit_only_quality_flags")
        if not isinstance(flags, list) or any(
            not isinstance(flag, str) for flag in flags
        ):
            raise SystemExit("generation_quality_flags_invalid")

        try:
            reconstructed = runner.NaturalInterviewerOutput.model_validate(
                {
                    "interviewer_message": packet_round["assistant_text"],
                    "session_action": packet_round["session_action"],
                    "finish_reason": packet_round["finish_reason"],
                    "navigation": record.get("navigation"),
                }
            )
            runner._validate_scenario_contract(
                scenario=packet_round["scenario"],
                round_index=round_index,
                output=reconstructed,
                prompt_version=str(version),
                transcript=packet_round["transcript_before_response"],
            )
        except BaseException as exc:
            raise SystemExit("generation_reconstructed_contract_invalid") from exc
        records_by_version[str(version)].append(record)

    if seen_records != set(packet_rounds):
        raise SystemExit("generation_logical_record_coverage_invalid")
    expected_calls_per_version = runner.NORMAL_LOGICAL_CALLS // len(
        runner.PROMPT_VERSIONS
    )
    if any(
        len(version_records) != expected_calls_per_version
        for version_records in records_by_version.values()
    ):
        raise SystemExit("generation_version_call_balance_invalid")
    if sum(
        int(record["physical_requests_for_logical_call"]) for record in records
    ) != manifest["physical_requests_observed"]:
        raise SystemExit("generation_physical_count_mismatch")
    if sum(bool(record["repair_used"]) for record in records) != manifest.get(
        "repair_calls_observed"
    ):
        raise SystemExit("generation_repair_count_mismatch")
    return records_by_version


def _runtime_metrics(
    records_by_version: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for version, records in records_by_version.items():
        repair_count = sum(bool(record["repair_used"]) for record in records)
        latencies = [int(record["latency_ms"]) for record in records]
        metrics[version] = {
            "logical_call_count": len(records),
            "repair_count": repair_count,
            "repair_rate": round(repair_count / len(records), 4),
            "mean_attempt_count": round(
                fmean(int(record["attempt_count"]) for record in records), 4
            ),
            "p95_latency_ms_inclusive": round(
                quantiles(latencies, n=20, method="inclusive")[-1], 3
            ),
        }

    baseline = metrics["v6.2.1"]
    candidate = metrics["v6.2.3"]
    repair_limit = min(
        MAX_V623_REPAIR_RATE,
        float(baseline["repair_rate"]) + REPAIR_RATE_MARGIN,
    )
    attempt_limit = min(
        MAX_V623_MEAN_ATTEMPTS,
        float(baseline["mean_attempt_count"]) + MEAN_ATTEMPT_MARGIN,
    )
    latency_limit = min(
        MAX_V623_P95_LATENCY_MS,
        max(
            float(baseline["p95_latency_ms_inclusive"]) * P95_LATENCY_RATIO,
            float(baseline["p95_latency_ms_inclusive"])
            + P95_LATENCY_MARGIN_MS,
        ),
    )
    gates = {
        "repair_rate": {
            "passed": float(candidate["repair_rate"]) <= repair_limit,
            "candidate": candidate["repair_rate"],
            "maximum": round(repair_limit, 4),
        },
        "mean_attempt_count": {
            "passed": float(candidate["mean_attempt_count"]) <= attempt_limit,
            "candidate": candidate["mean_attempt_count"],
            "maximum": round(attempt_limit, 4),
        },
        "p95_latency_ms_inclusive": {
            "passed": float(candidate["p95_latency_ms_inclusive"])
            <= latency_limit,
            "candidate": candidate["p95_latency_ms_inclusive"],
            "maximum": round(latency_limit, 3),
        },
    }
    return metrics, {
        "passed": all(item["passed"] for item in gates.values()),
        "gates": gates,
    }


def adjudicate(run_dir: Path, review_paths: list[Path]) -> dict[str, Any]:
    if len(review_paths) != 2:
        raise SystemExit("exactly_two_blind_reviews_required")
    packet_path = run_dir / "blind_review_packet.json"
    manifest_path = run_dir / "run_manifest.json"
    events_path = run_dir / "events.jsonl"
    mapping_path = run_dir / "sealed" / "opaque_mapping.json"
    output_path = run_dir / "sealed" / "v623_blind_adjudication.json"
    if output_path.exists():
        raise SystemExit("adjudication_output_must_not_exist")

    packet = _load_object(packet_path)
    manifest = _load_object(manifest_path)
    mapping = _load_object(mapping_path)
    if manifest.get("status") != "completed":
        raise SystemExit("generation_run_not_completed")
    if manifest.get("protocol") != runner.RUNNER_PROTOCOL:
        raise SystemExit("generation_protocol_mismatch")
    if manifest.get("logical_calls_completed") != runner.NORMAL_LOGICAL_CALLS:
        raise SystemExit("generation_logical_call_plan_incomplete")
    records = manifest.get("logical_call_records")
    if not isinstance(records, list) or len(records) != runner.NORMAL_LOGICAL_CALLS:
        raise SystemExit("generation_logical_records_incomplete")
    if manifest.get("normal_logical_call_plan") != runner.NORMAL_LOGICAL_CALLS:
        raise SystemExit("generation_declared_plan_mismatch")
    if manifest.get("physical_request_cap") != runner.MAX_PHYSICAL_REQUESTS:
        raise SystemExit("generation_physical_cap_mismatch")
    physical_observed = manifest.get("physical_requests_observed")
    if (
        isinstance(physical_observed, bool)
        or not isinstance(physical_observed, int)
        or not 0 <= physical_observed <= runner.MAX_PHYSICAL_REQUESTS
    ):
        raise SystemExit("generation_physical_count_invalid")
    if manifest.get("expected_api_raw_model") != runner.EXPECTED_MODEL:
        raise SystemExit("generation_model_profile_mismatch")
    if manifest.get("expected_provider") != runner.EXPECTED_PROVIDER:
        raise SystemExit("generation_provider_profile_mismatch")
    if manifest.get("expected_thinking") != runner.EXPECTED_THINKING:
        raise SystemExit("generation_thinking_profile_mismatch")
    if manifest.get("expected_max_tokens") != runner.EXPECTED_MAX_TOKENS:
        raise SystemExit("generation_token_profile_mismatch")
    if manifest.get("synthetic_only") is not True:
        raise SystemExit("generation_must_be_synthetic_only")
    if manifest.get("reads_application_database") is not False:
        raise SystemExit("generation_database_boundary_mismatch")
    if manifest.get("automated_scoring_performed") is not False:
        raise SystemExit("generation_scoring_boundary_mismatch")
    _validate_generation_provenance(manifest)
    events = _load_and_verify_events(
        events_path=events_path,
        manifest=manifest,
    )
    artifact_sha256 = manifest.get("artifact_sha256")
    expected_artifact_hashes = {
        "blind_review_packet.json": private_io._sha256_file(packet_path),
        "blind_review_template.json": private_io._sha256_file(
            run_dir / "blind_review_template.json"
        ),
        "sealed/opaque_mapping.json": private_io._sha256_file(mapping_path),
    }
    if artifact_sha256 != expected_artifact_hashes:
        raise SystemExit("generation_artifact_hash_mismatch")
    if packet.get("protocol") != runner.RUNNER_PROTOCOL:
        raise SystemExit("blind_packet_protocol_mismatch")
    if packet.get("synthetic_only") is not True:
        raise SystemExit("blind_packet_must_be_synthetic_only")
    if packet.get("automated_scoring_performed") is not False:
        raise SystemExit("blind_packet_scoring_boundary_mismatch")
    if packet.get("review_dimensions") != list(runner.REVIEW_DIMENSIONS):
        raise SystemExit("blind_packet_review_dimensions_mismatch")
    packet_sha256 = private_io._sha256_file(packet_path)
    expected = _expected_candidates(packet)
    expected_case_count = len(runner.SCENARIOS) * runner.REPLICATES_PER_SCENARIO
    if len(expected) != expected_case_count:
        raise SystemExit("blind_packet_case_plan_incomplete")
    for case in packet["cases"]:
        for candidate in case["candidates"]:
            rounds = candidate.get("rounds")
            if not isinstance(rounds, list) or len(rounds) != runner.ROUNDS_PER_ARM:
                raise SystemExit("blind_packet_round_plan_incomplete")
    arm_key, scenario_by_case, replicate_by_case = _validated_mapping(
        mapping, expected
    )
    records_by_version = _validate_logical_records(
        manifest=manifest,
        packet=packet,
        arm_key=arm_key,
        scenario_by_case=scenario_by_case,
    )
    _replay_completed_event_chain(
        events=events,
        manifest=manifest,
        scenario_by_case=scenario_by_case,
        replicate_by_case=replicate_by_case,
    )
    runtime_metrics, runtime_noninferiority = _runtime_metrics(records_by_version)
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

    ratings: dict[str, dict[str, list[int]]] = {
        version: {dimension: [] for dimension in runner.REVIEW_DIMENSIONS}
        for version in runner.PROMPT_VERSIONS
    }
    severe_scenarios: dict[str, dict[str, set[str]]] = {
        version: {
            code: set() for code in runner.SEVERE_REGRESSION_CODES
        }
        for version in runner.PROMPT_VERSIONS
    }
    preference_votes = {version: 0 for version in runner.PROMPT_VERSIONS}
    tie_votes = 0

    for review in reviews:
        for case in review["cases"]:
            case_id = str(case["case_id"])
            if case["tie"]:
                tie_votes += 1
            else:
                preferred_key = (case_id, str(case["preferred_candidate_id"]))
                preference_votes[arm_key[preferred_key]] += 1
            for candidate in case["candidates"]:
                key = (case_id, str(candidate["candidate_id"]))
                version = arm_key[key]
                for dimension, value in candidate["ratings"].items():
                    ratings[version][dimension].append(int(value))
                for code in candidate["severe_regression_codes"]:
                    severe_scenarios[version][code].add(scenario_by_case[case_id])

    means = {
        version: {
            dimension: round(fmean(values), 4)
            for dimension, values in dimensions.items()
        }
        for version, dimensions in ratings.items()
    }
    deltas = {
        dimension: round(
            means["v6.2.3"][dimension] - means["v6.2.1"][dimension], 4
        )
        for dimension in runner.REVIEW_DIMENSIONS
    }
    primary_composite_delta = round(
        fmean(deltas[dimension] for dimension in PRIMARY_IMPROVEMENT_DIMENSIONS),
        4,
    )
    primary_no_regression = all(
        deltas[dimension] >= 0 for dimension in PRIMARY_IMPROVEMENT_DIMENSIONS
    )
    primary_meaningful_gain_count = sum(
        deltas[dimension] >= PER_DIMENSION_MEANINGFUL_GAIN
        for dimension in PRIMARY_IMPROVEMENT_DIMENSIONS
    )
    core_noninferiority = {
        dimension: deltas[dimension] >= -CORE_NONINFERIORITY_MARGIN
        for dimension in CORE_NONINFERIORITY_DIMENSIONS
    }
    severe_scenario_counts = {
        version: {
            code: len(scenarios) for code, scenarios in codes.items()
        }
        for version, codes in severe_scenarios.items()
    }
    v623_immediate_stop_codes = sorted(
        code
        for code in IMMEDIATE_STOP_CODES
        if severe_scenario_counts["v6.2.3"][code] > 0
    )
    v623_repeated_stop_codes = sorted(
        code
        for code, count in severe_scenario_counts["v6.2.3"].items()
        if code not in IMMEDIATE_STOP_CODES and count >= 2
    )
    preference_gate = preference_votes["v6.2.3"] > preference_votes["v6.2.1"]
    eligible = (
        primary_no_regression
        and primary_composite_delta >= PRIMARY_COMPOSITE_MINIMUM_GAIN
        and primary_meaningful_gain_count >= 2
        and all(core_noninferiority.values())
        and preference_gate
        and not v623_immediate_stop_codes
        and not v623_repeated_stop_codes
        and runtime_noninferiority["passed"]
    )
    result = {
        "protocol": runner.RUNNER_PROTOCOL,
        "blind_packet_sha256": packet_sha256,
        "reviewer_ids": reviewer_ids,
        "mean_ratings_by_version": means,
        "mean_delta_v623_minus_v621": deltas,
        "primary_improvement_dimensions": list(PRIMARY_IMPROVEMENT_DIMENSIONS),
        "primary_composite_delta": primary_composite_delta,
        "primary_no_regression": primary_no_regression,
        "primary_meaningful_gain_count": primary_meaningful_gain_count,
        "core_noninferiority_margin": CORE_NONINFERIORITY_MARGIN,
        "core_noninferiority_by_dimension": core_noninferiority,
        "preference_votes_by_version": preference_votes,
        "tie_votes": tie_votes,
        "preference_gate_passed": preference_gate,
        "severe_scenario_counts_by_version": severe_scenario_counts,
        "v623_immediate_stop_codes": v623_immediate_stop_codes,
        "v623_repeated_stop_codes": v623_repeated_stop_codes,
        "runtime_metrics_by_version": runtime_metrics,
        "runtime_noninferiority": runtime_noninferiority,
        "v623_eligible_for_default": eligible,
        "decision": "PROMOTE_V623" if eligible else "KEEP_V621",
        "automated_model_judge_used": False,
        "interpretation": (
            "Small synthetic blinded qualitative gate; not a validity, reliability, "
            "or statistical superiority claim."
        ),
    }
    private_io._write_private_json(output_path, result)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--review", required=True, action="append", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = adjudicate(
        args.run_dir.resolve(), [path.resolve() for path in args.review]
    )
    print(
        json.dumps(
            {
                "decision": result["decision"],
                "v623_eligible_for_default": result["v623_eligible_for_default"],
                "v623_immediate_stop_codes": result["v623_immediate_stop_codes"],
                "v623_repeated_stop_codes": result["v623_repeated_stop_codes"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
