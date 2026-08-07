from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas import NaturalInterviewerOutput
from scripts import compare_natural_interviewer_prompts_v605 as base
from scripts import compare_natural_interviewer_prompts_v610 as runner


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    def _typed_call(self, **_: object) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            output=NaturalInterviewerOutput(
                interviewer_message="回到刚才那次决定，当时哪条信息对你的判断影响最大？",
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


def test_v610_plan_is_eight_by_two_by_three_with_bounded_requests() -> None:
    assert len(runner.SCENARIOS) == 8
    assert runner.PROMPT_VERSIONS == ("v6.0.5", "v6.1.0")
    assert runner.ROUNDS_PER_ARM == 3
    assert runner.NORMAL_LOGICAL_CALLS == 48
    assert runner.MAX_PHYSICAL_REQUESTS == 80
    assert len({item.scenario_type for item in runner.SCENARIOS}) == 8


def test_protocol_context_restores_v605_runner_state() -> None:
    snapshot = {name: getattr(base, name) for name in runner._OVERRIDDEN_NAMES}

    with runner.protocol_context():
        assert base.PROMPT_VERSIONS == runner.PROMPT_VERSIONS
        assert base.RUNNER_PROTOCOL == runner.RUNNER_PROTOCOL

    assert all(getattr(base, name) is value for name, value in snapshot.items())


def test_missing_confirmation_stays_zero_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "must-not-exist"
    calls = 0

    def forbidden_post(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(base.base.gateway_module.httpx, "post", forbidden_post)
    with pytest.raises(SystemExit, match="missing_or_invalid_confirmation"):
        runner.execute(["--execute-real", "--output-dir", str(target)])
    assert calls == 0
    assert not target.exists()


def test_v610_outputs_are_blind_and_keep_version_mapping_sealed(tmp_path: Path) -> None:
    output = tmp_path / "private-v610-run"
    with runner.protocol_context(), base.protocol_context():
        base.base.create_private_directory(output)
        base.base.create_private_directory(output / "sealed")
        ledger = base.base.PrivateEventLedger(output / "events.jsonl")
        gateway = FakeGateway()
        try:
            manifest = base.base.run_plan(
                output_dir=output,
                gateway=gateway,  # type: ignore[arg-type]
                ledger=ledger,
                physical_audit=CountOnlyAudit(),  # type: ignore[arg-type]
            )
            base._write_blind_review_template(output)
        finally:
            ledger.close()

    assert gateway.calls == runner.NORMAL_LOGICAL_CALLS
    assert manifest["logical_calls_completed"] == runner.NORMAL_LOGICAL_CALLS
    packet_text = (output / "blind_review_packet.json").read_text(encoding="utf-8")
    mapping_text = (output / "sealed" / "opaque_mapping.json").read_text(encoding="utf-8")
    assert "prompt_version" not in packet_text
    assert "v6.0.5" not in packet_text
    assert "v6.1.0" not in packet_text
    assert "v6.0.5" in mapping_text
    assert "v6.1.0" in mapping_text
    packet = json.loads(packet_text)
    assert len(packet["cases"]) == len(runner.SCENARIOS)
    template = json.loads((output / "blind_review_template.json").read_text(encoding="utf-8"))
    assert set(template["cases"][0]["candidates"][0]["ratings"]) == set(
        runner.REVIEW_DIMENSIONS
    )


def test_audit_flags_length_internal_leak_and_free_chat_without_rewriting() -> None:
    message = "问题界定和证据评估会影响覆盖率。想聊什么都可以。" + "请继续具体说明。" * 12
    flags = runner.audit_only_flags(message, "我还没想好。", [])

    assert "normal_reply_over_90_visible_characters" in flags
    assert "internal_measurement_language_visible" in flags
    assert "free_chat_invitation" in flags
