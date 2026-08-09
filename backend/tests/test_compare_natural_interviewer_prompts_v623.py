from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.schemas import NaturalInterviewerOutput
from app.services.model_gateway import (
    build_interview_anchor_candidates,
    source_clarification_required,
)
from scripts import compare_natural_interviewer_prompts_v623 as runner


class CountOnlyAudit:
    count = 0
    usages: list[dict[str, Any]] = []


class FakeGateway:
    def __init__(
        self,
        physical_audit: runner.AsyncPhysicalCallAudit | None = None,
    ) -> None:
        self.calls = 0
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self.physical_audit = physical_audit

    def generate_interviewer(
        self,
        payload: dict[str, Any],
        *,
        prompt_version: str,
    ) -> SimpleNamespace:
        self.calls += 1
        self.payloads.append((prompt_version, payload))
        if self.physical_audit is not None:
            asyncio.run(
                self.physical_audit(
                    runner.OFFICIAL_DEEPSEEK_COMPLETION_URL,
                    json={
                        "model": runner.EXPECTED_MODEL,
                        "thinking": {"type": runner.EXPECTED_THINKING},
                        "max_tokens": runner.EXPECTED_MAX_TOKENS,
                        "messages": [
                            {
                                "role": "user",
                                "content": json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            }
                        ],
                    },
                )
            )
        assert payload["anchor_candidates"] == build_interview_anchor_candidates(
            payload["transcript"]
        )
        assert payload["source_clarification_required"] is source_clarification_required(
            payload["transcript"]
        )
        latest = str(payload["transcript"][-1]["content"])
        finish = "想结束访谈并生成报告" in latest
        candidate = payload["anchor_candidates"][-1]
        if payload["source_clarification_required"]:
            focus_kind = "source_ownership"
            relation = "source_clarification"
            message = "哪些内容来自 AI 或论文，哪些是你自己的判断与采纳理由？"
        else:
            focus_kind = "basis"
            relation = "core"
            message = "如果回到这件具体事情，哪条信息最可能改变你现在的判断？"
        if finish:
            message = "好的，谢谢你认真说完这些，我们就先在这里结束。"
        output = NaturalInterviewerOutput.model_validate(
            {
                "interviewer_message": message,
                "session_action": "finish" if finish else "continue",
                "finish_reason": "user_requested" if finish else None,
                "navigation": {
                    "decision_anchor": {
                        "turn_index": candidate["turn_index"],
                        "quote": candidate["quote"],
                        "start": candidate["start"],
                        "end": candidate["end"],
                        "text_hash": None,
                    },
                    "focus_kind": focus_kind,
                    "mainline_relation": relation,
                },
            }
        )
        return SimpleNamespace(
            output=output,
            provider="fake",
            model=runner.EXPECTED_MODEL,
            repair_used=False,
            latency_ms=1,
            attempt_count=1,
        )


class FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {
            "id": "response-id",
            "model": runner.EXPECTED_MODEL,
            "choices": [{"message": {"content": "{}"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_cache_hit_tokens": 8,
                "prompt_cache_miss_tokens": 2,
            },
        }


def make_private_output(tmp_path: Path) -> Path:
    output = tmp_path / "private-v623-run"
    runner.private_io.create_private_directory(output)
    runner.private_io.create_private_directory(output / "sealed")
    return output


def test_plan_is_eight_scenarios_two_replicates_two_arms_three_rounds() -> None:
    assert len(runner.SCENARIOS) == 8
    assert runner.REPLICATES_PER_SCENARIO == 2
    assert runner.PROMPT_VERSIONS == ("v6.2.1", "v6.2.3")
    assert runner.ROUNDS_PER_ARM == 3
    assert runner.NORMAL_LOGICAL_CALLS == 96
    assert runner.MAX_PHYSICAL_REQUESTS == 144
    assert len({item.scenario_type for item in runner.SCENARIOS}) == 8
    assert all(len(item.user_turns) == 3 for item in runner.SCENARIOS)


@pytest.mark.parametrize(
    "argv,error",
    [
        (["--output-dir", "{target}"], "missing_execute_real"),
        (
            ["--execute-real", "--output-dir", "{target}"],
            "missing_or_invalid_confirmation",
        ),
    ],
)
def test_missing_confirmation_is_zero_call_and_zero_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    error: str,
) -> None:
    target = tmp_path / "must-not-exist"
    network_calls = 0

    async def forbidden_post(*_: object, **__: object) -> None:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(runner.gateway_module, "_async_http_post", forbidden_post)
    rendered = [str(target) if item == "{target}" else item for item in argv]
    with pytest.raises(SystemExit, match=error):
        runner.execute(rendered)
    assert network_calls == 0
    assert not target.exists()


def test_repository_internal_output_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = runner.REPOSITORY_ROOT / ".forbidden-v623-output"
    network_calls = 0

    async def forbidden_post(*_: object, **__: object) -> None:
        nonlocal network_calls
        network_calls += 1

    monkeypatch.setattr(runner.gateway_module, "_async_http_post", forbidden_post)
    with pytest.raises(SystemExit, match="output_dir_must_be_outside_repository"):
        runner.execute(
            [
                "--execute-real",
                "--confirmation",
                runner.CONFIRMATION,
                "--output-dir",
                str(target),
            ]
        )
    assert network_calls == 0
    assert not target.exists()


def test_async_physical_audit_counts_before_network_and_locks_profile(
    tmp_path: Path,
) -> None:
    ledger = runner.private_io.PrivateEventLedger(tmp_path / "events.jsonl")
    network_calls = 0
    audit: runner.AsyncPhysicalCallAudit

    async def fake_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        assert audit.count == network_calls + 1
        network_calls += 1
        return FakeResponse()

    audit = runner.AsyncPhysicalCallAudit(fake_post, ledger, max_requests=1)
    request = {
        "model": runner.EXPECTED_MODEL,
        "thinking": {"type": runner.EXPECTED_THINKING},
        "max_tokens": runner.EXPECTED_MAX_TOKENS,
    }
    try:
        asyncio.run(
            audit(runner.OFFICIAL_DEEPSEEK_COMPLETION_URL, json=request)
        )
        with pytest.raises(runner.RunnerHardStop, match="budget_exhausted"):
            asyncio.run(
                audit(runner.OFFICIAL_DEEPSEEK_COMPLETION_URL, json=request)
            )
    finally:
        ledger.close()
    assert audit.count == 1
    assert network_calls == 1
    assert runner._usage_totals(audit.usages)["total_tokens"] == 14
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    started = next(
        event for event in events if event["event"] == "physical_call_started"
    )
    assert started["request_url"] == runner.OFFICIAL_DEEPSEEK_COMPLETION_URL
    assert started["request_host"] == runner.OFFICIAL_DEEPSEEK_HOST
    assert started["request_url_sha256"] == runner.private_io._sha256_text(
        runner.OFFICIAL_DEEPSEEK_COMPLETION_URL
    )


def test_async_physical_audit_rejects_nonproduction_profile_before_network(
    tmp_path: Path,
) -> None:
    ledger = runner.private_io.PrivateEventLedger(tmp_path / "events.jsonl")
    network_calls = 0

    async def fake_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        network_calls += 1
        return FakeResponse()

    audit = runner.AsyncPhysicalCallAudit(fake_post, ledger)
    try:
        with pytest.raises(
            runner.RunnerHardStop, match="requested_thinking_profile_mismatch"
        ):
            asyncio.run(
                audit(
                    runner.OFFICIAL_DEEPSEEK_COMPLETION_URL,
                    json={
                        "model": runner.EXPECTED_MODEL,
                        "thinking": {"type": "enabled"},
                        "max_tokens": runner.EXPECTED_MAX_TOKENS,
                    },
                )
            )
    finally:
        ledger.close()
    assert audit.count == 0
    assert network_calls == 0


def test_async_physical_audit_rejects_nonofficial_url_before_network(
    tmp_path: Path,
) -> None:
    ledger = runner.private_io.PrivateEventLedger(tmp_path / "events.jsonl")
    network_calls = 0

    async def fake_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        network_calls += 1
        return FakeResponse()

    audit = runner.AsyncPhysicalCallAudit(fake_post, ledger)
    try:
        with pytest.raises(
            runner.RunnerHardStop,
            match="provider_request_url_mismatch",
        ):
            asyncio.run(
                audit(
                    "https://example.invalid/chat/completions",
                    json={
                        "model": runner.EXPECTED_MODEL,
                        "thinking": {"type": runner.EXPECTED_THINKING},
                        "max_tokens": runner.EXPECTED_MAX_TOKENS,
                    },
                )
            )
    finally:
        ledger.close()
    assert audit.count == 0
    assert network_calls == 0


def test_fake_gateway_with_async_physical_audit_cannot_complete(
    tmp_path: Path,
) -> None:
    output = make_private_output(tmp_path)
    ledger = runner.private_io.PrivateEventLedger(output / "events.jsonl")

    async def fake_post(*_: object, **__: object) -> FakeResponse:
        return FakeResponse()

    audit = runner.AsyncPhysicalCallAudit(fake_post, ledger)
    gateway = FakeGateway(audit)
    try:
        with pytest.raises(
            runner.RunnerHardStop,
            match="production_model_gateway_required",
        ):
            runner.run_plan(
                output_dir=output,
                gateway=gateway,  # type: ignore[arg-type]
                physical_audit=audit,
                ledger=ledger,
            )
    finally:
        ledger.close()

    assert gateway.calls == 0
    assert audit.count == 0
    assert not (output / "run_manifest.json").exists()
    assert not (output / "blind_review_packet.json").exists()


def test_non_audited_physical_boundary_cannot_produce_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.settings, "model_gateway_mode", "real")
    output = make_private_output(tmp_path)
    ledger = runner.private_io.PrivateEventLedger(output / "events.jsonl")
    try:
        with pytest.raises(
            runner.RunnerHardStop,
            match="production_physical_audit_required",
        ):
            runner.run_plan(
                output_dir=output,
                gateway=runner.ModelGatewayService(),
                physical_audit=CountOnlyAudit(),
                ledger=ledger,
            )
    finally:
        ledger.close()
    assert not (output / "run_manifest.json").exists()
    assert not (output / "blind_review_packet.json").exists()


@pytest.mark.parametrize(
    "provider,model,error",
    [
        (
            "fake-provider",
            runner.EXPECTED_MODEL,
            "structured_result_provider_mismatch",
        ),
        (
            runner.EXPECTED_PROVIDER,
            "fake-model",
            "structured_result_model_mismatch",
        ),
    ],
)
def test_structured_result_provider_and_model_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    error: str,
) -> None:
    monkeypatch.setattr(runner.settings, "model_gateway_mode", "real")
    output = make_private_output(tmp_path)
    ledger = runner.private_io.PrivateEventLedger(output / "events.jsonl")

    async def fake_post(*_: object, **__: object) -> FakeResponse:
        return FakeResponse()

    audit = runner.AsyncPhysicalCallAudit(fake_post, ledger)

    def fake_generate(
        _: object,
        payload: dict[str, Any],
        *,
        prompt_version: str,
    ) -> SimpleNamespace:
        asyncio.run(
            audit(
                runner.OFFICIAL_DEEPSEEK_COMPLETION_URL,
                json={
                    "model": runner.EXPECTED_MODEL,
                    "thinking": {"type": runner.EXPECTED_THINKING},
                    "max_tokens": runner.EXPECTED_MAX_TOKENS,
                },
            )
        )
        result = FakeGateway().generate_interviewer(
            payload,
            prompt_version=prompt_version,
        )
        result.provider = provider
        result.model = model
        return result

    monkeypatch.setattr(
        runner.ModelGatewayService,
        "generate_interviewer",
        fake_generate,
    )
    try:
        with pytest.raises(runner.RunnerHardStop, match=error):
            runner.run_plan(
                output_dir=output,
                gateway=runner.ModelGatewayService(),
                physical_audit=audit,
                ledger=ledger,
            )
    finally:
        ledger.close()
    manifest = json.loads(
        (output / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "stopped"
    assert manifest["physical_requests_observed"] == 1
    assert manifest["events_jsonl_event_count"] == 4
    assert not (output / "blind_review_packet.json").exists()


def test_only_official_deepseek_base_urls_are_allowed() -> None:
    for allowed in runner.OFFICIAL_DEEPSEEK_BASE_URL_ALLOWLIST:
        endpoint = runner.official_deepseek_endpoint(allowed)
        assert endpoint["completion_url"] == (
            runner.OFFICIAL_DEEPSEEK_COMPLETION_URL
        )
    for rejected in (
        "https://proxy.example.invalid/v1",
        "https://api.deepseek.com/",
    ):
        with pytest.raises(
            runner.RunnerHardStop,
            match="deepseek_base_url_must_be_official",
        ):
            runner.official_deepseek_endpoint(rejected)


def test_hard_gate_rejects_question_on_explicit_finish() -> None:
    scenario = runner.SCENARIOS[-1]
    user_text = scenario.user_turns[-1]
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": runner.CANONICAL_OPENING},
        {"turn_index": 1, "role": "user", "content": user_text},
    ]
    payload = runner.build_production_payload(transcript)
    candidate = payload["anchor_candidates"][-1]
    output = NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": "好的，我们现在结束。还有什么要补充的吗？",
            "session_action": "finish",
            "finish_reason": "user_requested",
            "navigation": {
                "decision_anchor": {**candidate, "text_hash": None},
                "focus_kind": "other",
                "mainline_relation": "core",
            },
        }
    )
    with pytest.raises(
        runner.RunnerHardStop, match="finish_response_must_not_ask_question"
    ):
        runner._validate_scenario_contract(
            scenario=scenario,
            round_index=3,
            output=output,
            prompt_version="v6.2.3",
            transcript=transcript,
        )


def test_question_free_baseline_is_reviewed_but_candidate_is_rejected() -> None:
    scenario = runner.SCENARIOS[0]
    user_text = scenario.user_turns[0]
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": runner.CANONICAL_OPENING},
        {"turn_index": 1, "role": "user", "content": user_text},
    ]
    payload = runner.build_production_payload(transcript)
    candidate = payload["anchor_candidates"][-1]
    output = NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": "谢谢你愿意把这些说出来。",
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": {**candidate, "text_hash": None},
                "focus_kind": "decision_problem",
                "mainline_relation": "core",
            },
        }
    )

    runner._validate_scenario_contract(
        scenario=scenario,
        round_index=1,
        output=output,
        prompt_version="v6.2.1",
        transcript=transcript,
    )
    assert "question_free_response" in runner.audit_only_flags(
        output.interviewer_message,
        user_text,
        [],
    )

    with pytest.raises(
        runner.RunnerHardStop,
        match="v623_normal_probe_must_have_exactly_one_question",
    ):
        runner._validate_scenario_contract(
            scenario=scenario,
            round_index=1,
            output=output,
            prompt_version="v6.2.3",
            transcript=transcript,
        )


def test_logical_failure_persists_private_diagnostic_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.settings, "model_gateway_mode", "real")
    output = make_private_output(tmp_path)
    ledger = runner.private_io.PrivateEventLedger(output / "events.jsonl")
    network_calls = 0

    async def forbidden_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        network_calls += 1
        return FakeResponse()

    def fail_generate(*_: object, **__: object) -> None:
        raise RuntimeError("synthetic-failure")

    monkeypatch.setattr(
        runner.ModelGatewayService,
        "generate_interviewer",
        fail_generate,
    )
    audit = runner.AsyncPhysicalCallAudit(forbidden_post, ledger)

    try:
        with pytest.raises(RuntimeError, match="synthetic-failure"):
            runner.run_plan(
                output_dir=output,
                gateway=runner.ModelGatewayService(),
                physical_audit=audit,
                ledger=ledger,
            )
    finally:
        ledger.close()

    manifest = json.loads(
        (output / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "stopped"
    assert manifest["logical_calls_completed"] == 0
    assert manifest["failure_context"]["scenario_type"] == (
        runner.SCENARIOS[0].scenario_type
    )
    assert manifest["failure_context"]["replicate_index"] == 1
    assert manifest["failure_context"]["prompt_version"] in runner.PROMPT_VERSIONS
    assert manifest["failure_context"]["round_index"] == 1
    assert manifest["events_jsonl_event_count"] == 2
    assert manifest["events_jsonl_sha256"] == runner.private_io._sha256_file(
        output / "events.jsonl"
    )
    partial_mapping = output / "sealed" / "partial_opaque_mapping.json"
    assert partial_mapping.exists()
    events = [
        json.loads(line)
        for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "logical_call_started",
        "logical_call_failed",
    ]
    assert not (output / "blind_review_packet.json").exists()
    assert network_calls == 0
    assert stat.S_IMODE((output / "run_manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(partial_mapping.stat().st_mode) == 0o600
