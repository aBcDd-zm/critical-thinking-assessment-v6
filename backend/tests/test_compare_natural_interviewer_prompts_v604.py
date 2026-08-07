from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas import NaturalInterviewerOutput
from scripts import compare_natural_interviewer_prompts_v604 as runner


class FakeResponse:
    def __init__(self, *, model: object = runner.EXPECTED_MODEL) -> None:
        self.status_code = 200
        self._body = {
            "id": "synthetic-response-id",
            "model": model,
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"interviewer_message":"我想再了解一个具体点：'
                            '哪条信息最可能改变你现在的判断？",'
                            '"session_action":"continue","finish_reason":null}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }

    def json(self) -> dict[str, object]:
        return self._body


class FakeGateway:
    def __init__(self, *, finish: bool = False) -> None:
        self.finish = finish
        self.calls = 0

    def _typed_call(self, **_: object) -> SimpleNamespace:
        self.calls += 1
        if self.finish:
            output = NaturalInterviewerOutput(
                interviewer_message="谢谢你把这些想法说出来，我们就先停在这里。",
                session_action="finish",
                finish_reason="natural_closure",
            )
        else:
            output = NaturalInterviewerOutput(
                interviewer_message="哪条信息最可能改变你现在的判断？",
                session_action="continue",
                finish_reason=None,
            )
        return SimpleNamespace(
            output=output,
            provider="fake",
            model=runner.EXPECTED_MODEL,
            repair_used=False,
            latency_ms=1,
        )


class CountOnlyAudit:
    count = 0


def make_private_output(tmp_path: Path) -> tuple[Path, runner.PrivateEventLedger]:
    output = tmp_path / "private-v604-run"
    runner.create_private_directory(output)
    runner.create_private_directory(output / "sealed")
    return output, runner.PrivateEventLedger(output / "events.jsonl")


def test_frozen_plan_is_eight_by_two_by_three_and_stable() -> None:
    assert len(runner.SCENARIOS) == 8
    assert len(runner.PROMPT_VERSIONS) == 2
    assert runner.ROUNDS_PER_ARM == 3
    assert runner.NORMAL_LOGICAL_CALLS == 48
    assert all(len(item.user_turns) == 3 for item in runner.SCENARIOS)
    first = runner._sha256_text(runner._canonical_json(runner.scenario_plan_payload()))
    second = runner._sha256_text(runner._canonical_json(runner.scenario_plan_payload()))
    assert first == second


def test_missing_confirmation_is_zero_call_and_creates_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "must-not-exist"
    calls = 0

    def forbidden_post(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(runner.gateway_module.httpx, "post", forbidden_post)
    with pytest.raises(SystemExit, match="missing_or_invalid_confirmation"):
        runner.execute(["--execute-real", "--output-dir", str(target)])
    assert calls == 0
    assert not target.exists()


def test_repository_internal_output_is_rejected_before_creation_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = runner.REPOSITORY_ROOT / ".forbidden-v604-private-output"
    calls = 0

    def forbidden_post(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(runner.gateway_module.httpx, "post", forbidden_post)
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
    assert calls == 0
    assert not target.exists()


def test_physical_budget_counts_before_post_and_fails_closed(tmp_path: Path) -> None:
    ledger = runner.PrivateEventLedger(tmp_path / "events.jsonl")
    network_calls = 0
    audit: runner.PhysicalCallAudit

    def fake_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        # The physical counter is already incremented before the network boundary.
        assert audit.count == network_calls + 1
        network_calls += 1
        return FakeResponse()

    audit = runner.PhysicalCallAudit(fake_post, ledger, max_requests=2)
    try:
        audit("https://example.invalid", json={"model": runner.EXPECTED_MODEL})
        audit("https://example.invalid", json={"model": runner.EXPECTED_MODEL})
        with pytest.raises(runner.RunnerHardStop, match="budget_exhausted"):
            audit("https://example.invalid", json={"model": runner.EXPECTED_MODEL})
    finally:
        ledger.close()
    assert network_calls == 2
    assert audit.count == 2


def test_api_raw_model_mismatch_is_immediate_hard_stop(tmp_path: Path) -> None:
    ledger = runner.PrivateEventLedger(tmp_path / "events.jsonl")
    network_calls = 0

    def fake_post(*_: object, **__: object) -> FakeResponse:
        nonlocal network_calls
        network_calls += 1
        return FakeResponse(model="unexpected-model")

    audit = runner.PhysicalCallAudit(fake_post, ledger)
    try:
        with pytest.raises(runner.RunnerHardStop, match="api_raw_model_mismatch"):
            audit("https://example.invalid", json={"model": runner.EXPECTED_MODEL})
    finally:
        ledger.close()
    assert network_calls == 1
    assert audit.count == 1


def test_private_outputs_are_blind_separate_and_mode_locked(tmp_path: Path) -> None:
    output, ledger = make_private_output(tmp_path)
    gateway = FakeGateway()
    try:
        manifest = runner.run_plan(
            output_dir=output,
            gateway=gateway,  # type: ignore[arg-type]
            ledger=ledger,
            physical_audit=CountOnlyAudit(),  # type: ignore[arg-type]
        )
    finally:
        ledger.close()

    assert gateway.calls == runner.NORMAL_LOGICAL_CALLS
    assert manifest["logical_calls_completed"] == runner.NORMAL_LOGICAL_CALLS
    packet_path = output / "blind_review_packet.json"
    mapping_path = output / "sealed" / "opaque_mapping.json"
    packet_text = packet_path.read_text(encoding="utf-8")
    mapping_text = mapping_path.read_text(encoding="utf-8")
    assert "prompt_version" not in packet_text
    assert "prompt_id" not in packet_text
    assert "audit_only_quality_flags" not in packet_text
    assert "v6.0.3" not in packet_text
    assert "v6.0.4" not in packet_text
    assert "prompt_version" in mapping_text
    assert "v6.0.3" in mapping_text
    assert "v6.0.4" in mapping_text
    packet = json.loads(packet_text)
    assert len(packet["cases"]) == 8
    assert all(len(case["candidates"]) == 2 for case in packet["cases"])

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "sealed").stat().st_mode) == 0o700
    for path in output.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_finish_stops_each_arm_without_later_rounds(tmp_path: Path) -> None:
    output, ledger = make_private_output(tmp_path)
    gateway = FakeGateway(finish=True)
    try:
        manifest = runner.run_plan(
            output_dir=output,
            gateway=gateway,  # type: ignore[arg-type]
            ledger=ledger,
            physical_audit=CountOnlyAudit(),  # type: ignore[arg-type]
        )
    finally:
        ledger.close()

    assert gateway.calls == len(runner.SCENARIOS) * len(runner.PROMPT_VERSIONS)
    assert manifest["logical_calls_completed"] == gateway.calls
    assert len(manifest["arms_finished_early"]) == gateway.calls
    packet = json.loads((output / "blind_review_packet.json").read_text(encoding="utf-8"))
    assert all(
        len(candidate["rounds"]) == 1
        for case in packet["cases"]
        for candidate in case["candidates"]
    )
