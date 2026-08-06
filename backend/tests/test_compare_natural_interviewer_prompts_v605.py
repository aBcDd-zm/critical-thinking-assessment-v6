from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas import NaturalInterviewerOutput
from scripts import compare_natural_interviewer_prompts_v604 as base
from scripts import compare_natural_interviewer_prompts_v605 as runner


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    def _typed_call(self, **_: object) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            output=NaturalInterviewerOutput(
                interviewer_message="这件事对你很重要。你愿意先从最在意的一点说起吗？",
                session_action="continue",
                finish_reason=None,
            ),
            provider="fake",
            model=runner.EXPECTED_MODEL,
            repair_used=False,
            latency_ms=1,
        )


class CountOnlyAudit:
    count = 0


def test_v605_plan_is_ten_by_two_by_three_with_eighty_request_cap() -> None:
    assert len(runner.SCENARIOS) == 10
    assert runner.PROMPT_VERSIONS == ("v6.0.4", "v6.0.5")
    assert runner.ROUNDS_PER_ARM == 3
    assert runner.NORMAL_LOGICAL_CALLS == 60
    assert runner.MAX_PHYSICAL_REQUESTS == 80
    assert len({item.scenario_type for item in runner.SCENARIOS}) == 10
    assert all(len(item.user_turns) == 3 for item in runner.SCENARIOS)


def test_protocol_context_restores_frozen_v604_module_state_and_source() -> None:
    source_path = base.REPOSITORY_ROOT / "backend/scripts/compare_natural_interviewer_prompts_v604.py"
    before = base._sha256_file(source_path)
    snapshot = {
        name: getattr(base, name)
        for name in runner._BASE_PROTOCOL_ATTRIBUTE_NAMES
    }

    with runner.protocol_context():
        assert base.RUNNER_PROTOCOL == runner.RUNNER_PROTOCOL
        assert base.PROMPT_VERSIONS == runner.PROMPT_VERSIONS

    assert base._sha256_file(source_path) == before
    assert all(getattr(base, name) is value for name, value in snapshot.items())


def test_missing_confirmation_is_zero_call_and_creates_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "must-not-exist"
    calls = 0

    def forbidden_post(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(base.gateway_module.httpx, "post", forbidden_post)
    with pytest.raises(SystemExit, match="missing_or_invalid_confirmation"):
        runner.execute(["--execute-real", "--output-dir", str(target)])
    assert calls == 0
    assert not target.exists()


def test_v605_outputs_remain_blind_private_and_separate(tmp_path: Path) -> None:
    output = tmp_path / "private-v605-run"
    with runner.protocol_context():
        base.create_private_directory(output)
        base.create_private_directory(output / "sealed")
        ledger = base.PrivateEventLedger(output / "events.jsonl")
        gateway = FakeGateway()
        try:
            manifest = base.run_plan(
                output_dir=output,
                gateway=gateway,  # type: ignore[arg-type]
                ledger=ledger,
                physical_audit=CountOnlyAudit(),  # type: ignore[arg-type]
            )
            runner._write_blind_review_template(output)
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
    assert "v6.0.4" not in packet_text
    assert "v6.0.5" not in packet_text
    assert "v604" not in packet_text
    assert "v605" not in packet_text
    assert "v6.0.4" in mapping_text
    assert "v6.0.5" in mapping_text
    packet = json.loads(packet_text)
    assert len(packet["cases"]) == 10
    assert all(len(case["candidates"]) == 2 for case in packet["cases"])
    template = json.loads((output / "blind_review_template.json").read_text(encoding="utf-8"))
    assert template["protocol"] == runner.RUNNER_PROTOCOL
    assert template["blind_packet_sha256"] == base._sha256_file(packet_path)
    assert set(template["severe_regression_codes"]) == set(runner.SEVERE_REGRESSION_CODES)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for path in output.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_audit_flags_only_detect_cross_turn_shape_and_assertive_affect() -> None:
    prior = [
        "你说这件事持续了一阵子，我想了解它最先影响了哪一部分？",
        "这似乎让计划变得复杂，你会先核实哪条信息？",
    ]
    flags = runner.audit_only_flags(
        "你现在一定很难受，我想知道接下来最需要确认什么？",
        "我还没想清楚。",
        prior,
    )

    assert "three_consecutive_preface_question_shape" in flags
    assert "assertive_inferred_affect" in flags


def test_response_only_turn_does_not_trigger_question_shape_flag() -> None:
    flags = runner.audit_only_flags(
        "可以，我们先停一下。你不用现在把所有事情想清楚。",
        "能先别继续问吗？",
        [
            "你提到最近很忙，我想知道最难安排的是哪件事？",
            "听起来你有很多顾虑，你会先处理哪个问题？",
        ],
    )

    assert "three_consecutive_preface_question_shape" not in flags
