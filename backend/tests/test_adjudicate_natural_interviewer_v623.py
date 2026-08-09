from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from scripts import adjudicate_natural_interviewer_v623 as adjudicator
from scripts import compare_natural_interviewer_prompts_v623 as runner


def write_valid_event_chain(
    run_dir: Path,
    manifest: dict[str, Any],
) -> None:
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()
    ledger = runner.private_io.PrivateEventLedger(events_path)
    physical_request_index = 0
    event_count = 0
    try:
        for record in manifest["logical_call_records"]:
            case_index = int(str(record["case_id"]).removeprefix("case_"))
            scenario = runner.SCENARIOS[
                case_index // runner.REPLICATES_PER_SCENARIO
            ]
            context = {
                "scenario_type": scenario.scenario_type,
                "replicate_index": (
                    case_index % runner.REPLICATES_PER_SCENARIO + 1
                ),
                "prompt_version": record["prompt_version"],
                "round_index": record["round_index"],
                "case_id": record["case_id"],
                "candidate_id": record["candidate_id"],
            }
            ledger.append({"event": "logical_call_started", **context})
            event_count += 1
            attempt_count = int(record["attempt_count"])
            record["physical_requests_for_logical_call"] = attempt_count
            record["repair_used"] = attempt_count > 1
            raw_hashes: list[str] = []
            for attempt_index in range(1, attempt_count + 1):
                physical_request_index += 1
                request_hash = runner.private_io._sha256_text(
                    runner.private_io._canonical_json(
                        {
                            "case_id": record["case_id"],
                            "candidate_id": record["candidate_id"],
                            "round_index": record["round_index"],
                            "attempt_index": attempt_index,
                        }
                    )
                )
                raw_hash = runner.private_io._sha256_text(
                    f"provider-response:{physical_request_index}"
                )
                raw_hashes.append(raw_hash)
                ledger.append(
                    {
                        "event": "physical_call_started",
                        "physical_request_index": physical_request_index,
                        "physical_request_cap": runner.MAX_PHYSICAL_REQUESTS,
                        "request_url": manifest["provider_completion_url"],
                        "request_url_sha256": manifest[
                            "provider_completion_url_sha256"
                        ],
                        "request_host": manifest["provider_host"],
                        "request_host_sha256": manifest[
                            "provider_host_sha256"
                        ],
                        "request_payload_sha256": request_hash,
                        "requested_model": runner.EXPECTED_MODEL,
                        "requested_thinking": {
                            "type": runner.EXPECTED_THINKING
                        },
                        "requested_max_tokens": runner.EXPECTED_MAX_TOKENS,
                    }
                )
                ledger.append(
                    {
                        "event": "physical_call_completed",
                        "physical_request_index": physical_request_index,
                        "http_status_code": 200,
                        "api_raw_model": runner.EXPECTED_MODEL,
                        "raw_content_sha256": raw_hash,
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "total_tokens": 14,
                        },
                    }
                )
                event_count += 2
            record["provider"] = runner.EXPECTED_PROVIDER
            record["model"] = runner.EXPECTED_MODEL
            record["api_raw_model"] = runner.EXPECTED_MODEL
            record["provider_request_url"] = manifest[
                "provider_completion_url"
            ]
            record["provider_request_url_sha256"] = manifest[
                "provider_completion_url_sha256"
            ]
            record["provider_request_host"] = manifest["provider_host"]
            record["provider_request_host_sha256"] = manifest[
                "provider_host_sha256"
            ]
            record["physical_response_raw_content_sha256"] = raw_hashes
            ledger.append(
                {
                    "event": "logical_call_completed",
                    **context,
                    "output_text_sha256": record["output_text_sha256"],
                    "session_action": record["session_action"],
                    "finish_reason": record["finish_reason"],
                    "provider": record["provider"],
                    "model": record["model"],
                    "api_raw_model": record["api_raw_model"],
                    "provider_request_url": record[
                        "provider_request_url"
                    ],
                    "provider_request_url_sha256": record[
                        "provider_request_url_sha256"
                    ],
                    "provider_request_host": record[
                        "provider_request_host"
                    ],
                    "provider_request_host_sha256": record[
                        "provider_request_host_sha256"
                    ],
                    "physical_response_raw_content_sha256": raw_hashes,
                    "repair_used": record["repair_used"],
                    "attempt_count": record["attempt_count"],
                    "latency_ms": record["latency_ms"],
                    "physical_requests_for_logical_call": record[
                        "physical_requests_for_logical_call"
                    ],
                }
            )
            event_count += 1
    finally:
        ledger.close()
    manifest["physical_requests_observed"] = physical_request_index
    manifest["repair_calls_observed"] = sum(
        bool(record["repair_used"])
        for record in manifest["logical_call_records"]
    )
    manifest["events_jsonl_sha256"] = runner.private_io._sha256_file(
        events_path
    )
    manifest["events_jsonl_event_count"] = event_count


def make_run(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, str]]:
    run_dir = tmp_path / "private-run"
    runner.private_io.create_private_directory(run_dir)
    runner.private_io.create_private_directory(run_dir / "sealed")
    packet_cases: list[dict[str, Any]] = []
    case_key: list[dict[str, Any]] = []
    arm_key: list[dict[str, Any]] = []
    logical_records: list[dict[str, Any]] = []
    version_by_candidate: dict[str, str] = {}
    for index in range(len(runner.SCENARIOS) * runner.REPLICATES_PER_SCENARIO):
        scenario = runner.SCENARIOS[index // runner.REPLICATES_PER_SCENARIO]
        replicate_index = index % runner.REPLICATES_PER_SCENARIO + 1
        case_id = f"case_{index}"
        baseline_id = f"candidate_{index}_a"
        candidate_id = f"candidate_{index}_b"
        case_key.append(
            {
                "case_id": case_id,
                "scenario_type": scenario.scenario_type,
                "replicate_index": replicate_index,
                "scenario_sha256": runner.private_io._sha256_text(
                    runner.private_io._canonical_json(
                        {
                            "user_turns": list(scenario.user_turns),
                            "round_policies": list(scenario.round_policies),
                        }
                    )
                ),
            }
        )
        for opaque_id, version in (
            (baseline_id, "v6.2.1"),
            (candidate_id, "v6.2.3"),
        ):
            prompt_id, resolved_version, prompt = (
                runner.resolve_natural_interviewer_prompt(version)
            )
            arm_key.append(
                {
                    "case_id": case_id,
                    "candidate_id": opaque_id,
                    "prompt_id": prompt_id,
                    "prompt_version": resolved_version,
                    "prompt_sha256": runner.private_io._sha256_text(prompt),
                }
            )
            version_by_candidate[opaque_id] = version
        candidates: list[dict[str, Any]] = []
        for opaque_id, version in (
            (baseline_id, "v6.2.1"),
            (candidate_id, "v6.2.3"),
        ):
            transcript: list[dict[str, Any]] = [
                {
                    "turn_index": 0,
                    "role": "assistant",
                    "content": runner.CANONICAL_OPENING,
                }
            ]
            rounds: list[dict[str, Any]] = []
            prior_outputs: list[str] = []
            for round_index, user_text in enumerate(scenario.user_turns, start=1):
                transcript.append(
                    {
                        "turn_index": len(transcript),
                        "role": "user",
                        "content": user_text,
                    }
                )
                payload = runner.build_production_payload(transcript)
                finish = scenario.round_policies[round_index - 1] == "finish"
                if finish:
                    assistant_text = "好的，谢谢你认真说完这些，我们就先在这里结束。"
                elif payload["source_clarification_required"]:
                    assistant_text = (
                        "哪些内容来自 AI 或论文，哪些是你自己的判断与采纳理由？"
                    )
                else:
                    assistant_text = (
                        "如果回到这件具体事情，哪条信息最可能改变你现在的判断？"
                    )
                candidate = payload["anchor_candidates"][-1]
                raw_navigation = {
                    "decision_anchor": {
                        "turn_index": candidate["turn_index"],
                        "quote": candidate["quote"],
                        "start": candidate["start"],
                        "end": candidate["end"],
                        "text_hash": None,
                    },
                    "focus_kind": (
                        "source_ownership"
                        if payload["source_clarification_required"]
                        else "basis"
                    ),
                    "mainline_relation": (
                        "source_clarification"
                        if payload["source_clarification_required"]
                        else "core"
                    ),
                }
                output = runner.NaturalInterviewerOutput.model_validate(
                    {
                        "interviewer_message": assistant_text,
                        "session_action": "finish" if finish else "continue",
                        "finish_reason": "user_requested" if finish else None,
                        "navigation": raw_navigation,
                    }
                )
                navigation = runner._validated_navigation(
                    output,
                    prompt_version=version,
                    transcript=transcript,
                )
                rounds.append(
                    {
                        "round_index": round_index,
                        "user_text": user_text,
                        "assistant_text": assistant_text,
                        "session_action": output.session_action,
                        "finish_reason": output.finish_reason,
                        "conversation_ended_after_this_turn": finish,
                    }
                )
                logical_records.append(
                    {
                        "case_id": case_id,
                        "candidate_id": opaque_id,
                        "prompt_version": version,
                        "round_index": round_index,
                        "input_payload_sha256": runner.private_io._sha256_text(
                            runner.private_io._canonical_json(payload)
                        ),
                        "output_text_sha256": runner.private_io._sha256_text(
                            assistant_text
                        ),
                        "session_action": output.session_action,
                        "finish_reason": output.finish_reason,
                        "repair_used": False,
                        "attempt_count": 1,
                        "latency_ms": 1_000 if version == "v6.2.1" else 900,
                        "physical_requests_for_logical_call": 1,
                        "audit_only_quality_flags": runner.audit_only_flags(
                            assistant_text, user_text, prior_outputs
                        ),
                        "navigation": navigation,
                    }
                )
                transcript.append(
                    {
                        "turn_index": len(transcript),
                        "role": "assistant",
                        "content": assistant_text,
                    }
                )
                prior_outputs.append(assistant_text)
            candidates.append(
                {
                    "candidate_id": opaque_id,
                    "opening": runner.CANONICAL_OPENING,
                    "rounds": rounds,
                }
            )
        packet_cases.append({"case_id": case_id, "candidates": candidates})
    packet = {
        "protocol": runner.RUNNER_PROTOCOL,
        "synthetic_only": True,
        "automated_scoring_performed": False,
        "review_dimensions": list(runner.REVIEW_DIMENSIONS),
        "cases": packet_cases,
    }
    template = {"protocol": runner.RUNNER_PROTOCOL}
    mapping = {
        "protocol": runner.RUNNER_PROTOCOL,
        "case_key": case_key,
        "candidate_arm_key": arm_key,
    }
    runner.private_io._write_private_json(
        run_dir / "blind_review_packet.json", packet
    )
    runner.private_io._write_private_json(
        run_dir / "blind_review_template.json", template
    )
    runner.private_io._write_private_json(
        run_dir / "sealed" / "opaque_mapping.json", mapping
    )
    manifest = runner._base_manifest(run_dir)
    manifest.update(
        {
            "status": "completed",
            "stop_reason": "normal_completion",
            "logical_calls_completed": runner.NORMAL_LOGICAL_CALLS,
            "logical_call_records": logical_records,
            "physical_requests_observed": runner.NORMAL_LOGICAL_CALLS,
            "repair_calls_observed": 0,
            "artifact_sha256": {
                "blind_review_packet.json": runner.private_io._sha256_file(
                    run_dir / "blind_review_packet.json"
                ),
                "blind_review_template.json": runner.private_io._sha256_file(
                    run_dir / "blind_review_template.json"
                ),
                "sealed/opaque_mapping.json": runner.private_io._sha256_file(
                    run_dir / "sealed" / "opaque_mapping.json"
                ),
            },
        }
    )
    write_valid_event_chain(run_dir, manifest)
    runner.private_io._write_private_json(run_dir / "run_manifest.json", manifest)
    return run_dir, packet, version_by_candidate


def make_review(
    *,
    run_dir: Path,
    packet: dict[str, Any],
    version_by_candidate: dict[str, str],
    reviewer_id: str,
    severe_code: str | None = None,
) -> Path:
    cases: list[dict[str, Any]] = []
    for case_index, case in enumerate(packet["cases"]):
        v623_id = next(
            item["candidate_id"]
            for item in case["candidates"]
            if version_by_candidate[item["candidate_id"]] == "v6.2.3"
        )
        candidates: list[dict[str, Any]] = []
        for item in case["candidates"]:
            version = version_by_candidate[item["candidate_id"]]
            ratings = {
                dimension: (
                    4
                    if version == "v6.2.3"
                    and dimension in adjudicator.PRIMARY_IMPROVEMENT_DIMENSIONS
                    else 3
                )
                for dimension in runner.REVIEW_DIMENSIONS
            }
            selected_severe = (
                [severe_code]
                if severe_code is not None
                and version == "v6.2.3"
                and case_index == 0
                else []
            )
            candidates.append(
                {
                    "candidate_id": item["candidate_id"],
                    "ratings": ratings,
                    "severe_regression_codes": selected_severe,
                    "problematic_round_indices": [1] if selected_severe else [],
                    "reviewer_notes": (
                        "Observed a concrete severe failure." if selected_severe else ""
                    ),
                }
            )
        cases.append(
            {
                "case_id": case["case_id"],
                "preferred_candidate_id": v623_id,
                "tie": False,
                "preference_reason": "The preferred candidate was warmer without losing focus.",
                "candidates": candidates,
            }
        )
    review = {
        "protocol": runner.RUNNER_PROTOCOL,
        "reviewer_id": reviewer_id,
        "blind_packet_sha256": runner.private_io._sha256_file(
            run_dir / "blind_review_packet.json"
        ),
        "cases": cases,
    }
    path = run_dir / f"review-{reviewer_id}.json"
    runner.private_io._write_private_json(path, review)
    return path


def test_adjudicator_promotes_only_after_two_complete_blind_reviews(
    tmp_path: Path,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )

    result = adjudicator.adjudicate(run_dir, [review_a, review_b])

    assert result["decision"] == "PROMOTE_V623"
    assert result["v623_eligible_for_default"] is True
    assert result["primary_composite_delta"] == 1.0
    assert result["primary_meaningful_gain_count"] == 4
    assert all(result["core_noninferiority_by_dimension"].values())
    assert result["preference_votes_by_version"] == {
        "v6.2.1": 0,
        "v6.2.3": 32,
    }
    assert result["runtime_noninferiority"]["passed"] is True
    assert result["runtime_metrics_by_version"]["v6.2.3"][
        "mean_attempt_count"
    ] == 1.0
    output = run_dir / "sealed" / "v623_blind_adjudication.json"
    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_single_immediate_severe_code_keeps_v621(tmp_path: Path) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
        severe_code="severe_overclaim_or_diagnosis",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )

    result = adjudicator.adjudicate(run_dir, [review_a, review_b])

    assert result["decision"] == "KEEP_V621"
    assert result["v623_eligible_for_default"] is False
    assert result["v623_immediate_stop_codes"] == [
        "severe_overclaim_or_diagnosis"
    ]


def test_reviews_must_have_distinct_reviewer_ids(tmp_path: Path) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="same-reviewer",
    )
    duplicate = run_dir / "duplicate-review.json"
    duplicate.write_text(review_a.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(SystemExit, match="reviewer_ids_must_be_distinct"):
        adjudicator.adjudicate(run_dir, [review_a, duplicate])


def test_severe_code_requires_problem_round_and_note(tmp_path: Path) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
        severe_code="severe_structure_or_no_question_failure",
    )
    review = json.loads(review_a.read_text(encoding="utf-8"))
    severe_candidate = next(
        candidate
        for case in review["cases"]
        for candidate in case["candidates"]
        if candidate["severe_regression_codes"]
    )
    severe_candidate["problematic_round_indices"] = []
    runner.private_io._write_private_json(review_a, review)
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )

    with pytest.raises(SystemExit, match="severe_code_requires_round_and_note"):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("missing", "generation_events_jsonl_missing"),
        ("tampered", "generation_events_hash_mismatch"),
        ("unsealed_manifest", "generation_events_hash_invalid"),
    ],
)
def test_adjudicator_rejects_missing_tampered_or_unsealed_events(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    events_path = run_dir / "events.jsonl"
    manifest_path = run_dir / "run_manifest.json"
    if mutation == "missing":
        events_path.unlink()
    elif mutation == "tampered":
        events_path.write_text(
            events_path.read_text(encoding="utf-8") + "{}\n",
            encoding="utf-8",
        )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["events_jsonl_sha256"] = None
        runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match=error):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


def test_adjudicator_rejects_forged_logical_event_completion(
    tmp_path: Path,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    events_path = run_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    logical_completion = next(
        event for event in events if event["event"] == "logical_call_completed"
    )
    logical_completion["api_raw_model"] = "forged-model"
    events_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["events_jsonl_sha256"] = runner.private_io._sha256_file(
        events_path
    )
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match="generation_logical_completion_mismatch"):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


def test_adjudicator_rejects_resealed_nonofficial_physical_url(
    tmp_path: Path,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    events_path = run_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    physical_start = next(
        event for event in events if event["event"] == "physical_call_started"
    )
    physical_start["request_url"] = "https://example.invalid/chat/completions"
    physical_start["request_url_sha256"] = runner.private_io._sha256_text(
        physical_start["request_url"]
    )
    events_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["events_jsonl_sha256"] = runner.private_io._sha256_file(
        events_path
    )
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match="generation_physical_start_invalid"):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


@pytest.mark.parametrize(
    "field,replacement,error",
    [
        ("source_sha256", {}, "generation_source_hash_mismatch"),
        ("prompt_assets", {}, "generation_prompt_assets_mismatch"),
        (
            "scenario_plan_sha256",
            "0" * 64,
            "generation_scenario_plan_hash_mismatch",
        ),
        (
            "provider_completion_url",
            "https://example.invalid/chat/completions",
            "generation_provider_endpoint_mismatch",
        ),
    ],
)
def test_adjudicator_recomputes_generation_provenance(
    tmp_path: Path,
    field: str,
    replacement: object,
    error: str,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = replacement
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match=error):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


@pytest.mark.parametrize(
    "field,replacement,error",
    [
        ("output_text_sha256", "0" * 64, "generation_output_text_hash_mismatch"),
        ("prompt_version", "v6.2.3", "generation_logical_record_version_mismatch"),
        ("provider", "fake-provider", "generation_provider_mismatch"),
        ("model", "fake-model", "generation_record_model_mismatch"),
        (
            "provider_request_url",
            "https://example.invalid/chat/completions",
            "generation_record_provider_endpoint_mismatch",
        ),
        ("attempt_count", 4, "generation_attempt_count_invalid"),
        ("latency_ms", -1, "generation_latency_invalid"),
    ],
)
def test_adjudicator_rejects_inconsistent_logical_record_fields(
    tmp_path: Path,
    field: str,
    replacement: object,
    error: str,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["logical_call_records"][0][field] = replacement
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match=error):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


def test_adjudicator_rejects_duplicate_logical_record_coverage(
    tmp_path: Path,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["logical_call_records"][-1] = dict(
        manifest["logical_call_records"][0]
    )
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match="generation_logical_record_coverage_invalid"):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


@pytest.mark.parametrize(
    "mapping_section,error",
    [
        ("case_key", "sealed_case_scenario_hash_mismatch"),
        ("candidate_arm_key", "sealed_arm_prompt_asset_mismatch"),
    ],
)
def test_adjudicator_recomputes_sealed_mapping_assets(
    tmp_path: Path,
    mapping_section: str,
    error: str,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    mapping_path = run_dir / "sealed" / "opaque_mapping.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if mapping_section == "case_key":
        mapping[mapping_section][0]["scenario_sha256"] = "0" * 64
    else:
        mapping[mapping_section][0]["prompt_sha256"] = "0" * 64
    runner.private_io._write_private_json(mapping_path, mapping)
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["sealed/opaque_mapping.json"] = (
        runner.private_io._sha256_file(mapping_path)
    )
    runner.private_io._write_private_json(manifest_path, manifest)

    with pytest.raises(SystemExit, match=error):
        adjudicator.adjudicate(run_dir, [review_a, review_b])


def test_runtime_regression_blocks_promotion_despite_blind_preference(
    tmp_path: Path,
) -> None:
    run_dir, packet, version_by_candidate = make_run(tmp_path)
    review_a = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-a",
    )
    review_b = make_review(
        run_dir=run_dir,
        packet=packet,
        version_by_candidate=version_by_candidate,
        reviewer_id="reviewer-b",
    )
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_records = [
        record
        for record in manifest["logical_call_records"]
        if record["prompt_version"] == "v6.2.3"
    ]
    for record in candidate_records:
        record["repair_used"] = True
        record["attempt_count"] = 2
        record["physical_requests_for_logical_call"] = 2
        record["latency_ms"] = 30_000
    manifest["repair_calls_observed"] = len(candidate_records)
    manifest["physical_requests_observed"] = sum(
        record["physical_requests_for_logical_call"]
        for record in manifest["logical_call_records"]
    )
    write_valid_event_chain(run_dir, manifest)
    runner.private_io._write_private_json(manifest_path, manifest)

    result = adjudicator.adjudicate(run_dir, [review_a, review_b])

    assert result["decision"] == "KEEP_V621"
    assert result["runtime_noninferiority"]["passed"] is False
    gates = result["runtime_noninferiority"]["gates"]
    assert gates["repair_rate"]["passed"] is False
    assert gates["mean_attempt_count"]["passed"] is False
    assert gates["p95_latency_ms_inclusive"]["passed"] is False
