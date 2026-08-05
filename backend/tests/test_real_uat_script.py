from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts import check_real_deepseek_v6 as uat


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _make_settlement_fixture(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "ledger-v1"
    target = tmp_path / "ledger-v2"
    source.mkdir(mode=0o700)
    os.chmod(source, 0o700)
    database_paths = [tmp_path / "history-a.sqlite3", tmp_path / "history-b.sqlite3"]
    namespaces = [_sha256(str(path.resolve()).encode()) for path in database_paths]
    record_keys: list[list[object]] = []
    provider_attempts: list[int] = []
    global_ordinal = 0
    for database_index, (database_path, count) in enumerate(
        zip(database_paths, (54, 17), strict=True)
    ):
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE agent_traces (
                id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
                action TEXT NOT NULL, model_provider TEXT NOT NULL,
                model_name TEXT NOT NULL, requested_model TEXT,
                actual_model TEXT, response_id TEXT, request_id TEXT,
                transport_retry_count INTEGER NOT NULL,
                renderer_status TEXT NOT NULL, repair_used INTEGER NOT NULL,
                fallback_used INTEGER NOT NULL, fallback_reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE scoring_runs (
                id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
                status TEXT NOT NULL, model_provider TEXT NOT NULL,
                model_name TEXT NOT NULL, requested_model TEXT,
                actual_model TEXT, response_id TEXT, request_id TEXT,
                transport_retry_count INTEGER NOT NULL,
                repair_used INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
            );
            """
        )
        for record_id in range(1, count + 1):
            global_ordinal += 1
            attempts = 2 if global_ordinal <= 4 else 1
            provider_attempts.append(attempts)
            connection.execute(
                """
                INSERT INTO agent_traces
                  (id, session_id, action, model_provider, model_name,
                   requested_model, actual_model, response_id, request_id,
                   transport_retry_count, renderer_status, repair_used,
                   fallback_used, fallback_reason, created_at)
                VALUES (?, 1, 'followup', 'deepseek', ?, ?, ?, ?, ?, ?,
                        'accepted', 0, 0, NULL, ?)
                """,
                (
                    record_id,
                    uat.EXPECTED_MODEL,
                    uat.EXPECTED_MODEL,
                    uat.EXPECTED_MODEL,
                    f"response-{global_ordinal}",
                    f"request-{global_ordinal}",
                    attempts - 1,
                    f"2026-08-05T00:{global_ordinal:02d}:00+00:00",
                ),
            )
            record_keys.append(
                [namespaces[database_index], "agent_trace", record_id]
            )
        connection.commit()
        connection.close()

    run_id = "sealed-run"
    events: list[dict[str, object]] = [
        {
            "event": "run_opened",
            "run_id": run_id,
            "record_namespace": namespaces[0],
            "observed_physical_attempts": 0,
            "reserved_physical_upper_bound": 0,
        }
    ]
    reserved = 0
    observed = 0
    for ordinal, (record_key, attempts) in enumerate(
        zip(record_keys, provider_attempts, strict=True), start=1
    ):
        reserved += 3
        namespace = str(record_key[0])
        events.append(
            {
                "event": "call_reserved",
                "run_id": run_id,
                "label": f"fixture:{ordinal}",
                "record_namespace": namespace,
                "reserved_attempts": 3,
                "observed_physical_attempts": observed,
                "reserved_physical_upper_bound": reserved,
            }
        )
        observed += attempts
        events.append(
            {
                "event": "audit_observed",
                "run_id": run_id,
                "record_namespace": namespace,
                "observed_physical_attempts": observed,
                "reserved_physical_upper_bound": reserved,
            }
        )
    events.append(
        {
            "event": "run_closed",
            "run_id": run_id,
            "record_namespace": namespaces[-1],
            "observed_physical_attempts": observed,
            "reserved_physical_upper_bound": reserved,
            "status": "FAILED",
        }
    )
    state = {
        "version": 1,
        "expected_model": uat.EXPECTED_MODEL,
        "expected_protocol": uat.EXPECTED_PROTOCOL,
        "soft_limit": uat.SOFT_REQUEST_WARNING,
        "pause_limit": uat.PAUSE_REQUEST_LIMIT,
        "hard_limit": uat.HARD_REQUEST_LIMIT,
        "observed_physical_attempts": observed,
        "reserved_physical_upper_bound": reserved,
        "record_keys": record_keys,
        "runs": [
            {
                "run_id": run_id,
                "record_namespace": namespaces[0],
                "opened_at": "2026-08-05T00:00:00Z",
            }
        ],
        "warned": False,
    }
    state_bytes = (json.dumps(state, sort_keys=True) + "\n").encode()
    event_bytes = (
        "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n"
    ).encode()
    (source / "budget_state.json").write_bytes(state_bytes)
    (source / "budget_events.jsonl").write_bytes(event_bytes)
    (source / "budget.lock").write_bytes(b"")
    for path in source.iterdir():
        os.chmod(path, 0o600)
    return {
        "source": source,
        "target": target,
        "databases": database_paths,
        "state_sha": _sha256(state_bytes),
        "events_sha": _sha256(event_bytes),
        "state_bytes": state_bytes,
        "event_bytes": event_bytes,
    }


def _make_aborted_disk_fixture(tmp_path: Path) -> dict[str, object]:
    base = _make_settlement_fixture(tmp_path)
    settled = uat.settle_budget_ledger_v1_to_v2(
        source_ledger_dir=base["source"],
        target_ledger_dir=base["target"],
        database_paths=base["databases"],
        expected_source_state_sha256=base["state_sha"],
        expected_source_events_sha256=base["events_sha"],
        confirmation=uat.SETTLEMENT_CONFIRMATION,
    )
    source = Path(base["target"])
    target = tmp_path / "ledger-v3"
    checkpoint = tmp_path / "forensic-checkpoint"
    failed_db = tmp_path / "failed.sqlite3"
    sealed = tmp_path / "sealed-failed"
    prompt = tmp_path / "orchestrator.py"
    gateway = tmp_path / "model_gateway.py"
    prompt.write_text("prompt-source\n")
    gateway.write_text("gateway-source\n")
    run_id = "disk-full-run"
    namespace = _sha256(str(failed_db.resolve()).encode())

    connection = sqlite3.connect(failed_db)
    connection.executescript(
        """
        CREATE TABLE agent_traces (
            id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
            action TEXT NOT NULL, model_provider TEXT NOT NULL,
            model_name TEXT NOT NULL, requested_model TEXT,
            actual_model TEXT, response_id TEXT, request_id TEXT,
            transport_retry_count INTEGER NOT NULL,
            renderer_status TEXT NOT NULL, repair_used INTEGER NOT NULL,
            fallback_used INTEGER NOT NULL, fallback_reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE scoring_runs (
            id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
            status TEXT NOT NULL, model_provider TEXT NOT NULL,
            model_name TEXT NOT NULL, requested_model TEXT,
            actual_model TEXT, response_id TEXT, request_id TEXT,
            transport_retry_count INTEGER NOT NULL,
            repair_used INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL
        );
        """
    )
    for record_id in range(1, uat.ABORT_SOURCE_NEW_PROVIDER_RECORDS + 1):
        connection.execute(
            """
            INSERT INTO agent_traces
              (id, session_id, action, model_provider, model_name,
               requested_model, actual_model, response_id, request_id,
               transport_retry_count, renderer_status, repair_used,
               fallback_used, fallback_reason, created_at)
            VALUES (?, 1, 'followup', 'deepseek', ?, ?, ?, ?, ?, ?,
                    'accepted', 0, 0, NULL, ?)
            """,
            (
                record_id,
                uat.EXPECTED_MODEL,
                uat.EXPECTED_MODEL,
                uat.EXPECTED_MODEL,
                f"abort-response-{record_id}",
                f"abort-request-{record_id}",
                1 if record_id == 20 else 0,
                f"2026-08-05T04:{record_id:02d}:00+00:00",
            ),
        )
    connection.commit()
    connection.close()
    Path(f"{failed_db}-wal").write_bytes(b"")
    Path(f"{failed_db}-shm").write_bytes(b"")

    state_path = source / "budget_state.json"
    events_path = source / "budget_events.jsonl"
    state = json.loads(state_path.read_text())
    prior_settlement_sha = str(settled["settlement_sha256"])
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    state["runs"] = [
        {
            "run_id": run_id,
            "record_namespace": namespace,
            "opened_at": "2026-08-05T04:00:00Z",
        }
    ]
    state["observed_physical_attempts"] = uat.ABORT_SOURCE_OBSERVED
    state["reserved_physical_upper_bound"] = uat.ABORT_SOURCE_RESERVED
    state["effective_committed_physical_upper_bound"] = uat.ABORT_SOURCE_RESERVED
    state["record_keys"].extend(
        [namespace, "agent_trace", record_id]
        for record_id in range(1, uat.ABORT_SOURCE_NEW_PROVIDER_RECORDS + 1)
    )
    events.append(
        {
            "event": "run_opened",
            "run_id": run_id,
            "record_namespace": namespace,
            "observed_physical_attempts": 75,
            "reserved_physical_upper_bound": 75,
        }
    )
    reserved = 75
    observed = 75
    for ordinal in range(1, uat.ABORT_SOURCE_NEW_RESERVATIONS + 1):
        reserved += 3
        events.append(
            {
                "event": "call_reserved",
                "run_id": run_id,
                "label": f"UAT-01:answer:{ordinal}",
                "record_namespace": namespace,
                "reserved_attempts": 3,
                "observed_physical_attempts": observed,
                "reserved_physical_upper_bound": reserved,
            }
        )
        if ordinal <= uat.ABORT_SOURCE_NEW_PROVIDER_RECORDS:
            observed += 2 if ordinal == 20 else 1
            events.append(
                {
                    "event": "audit_observed",
                    "run_id": run_id,
                    "record_namespace": namespace,
                    "observed_physical_attempts": observed,
                    "reserved_physical_upper_bound": reserved,
                }
            )
    state_bytes = (json.dumps(state, sort_keys=True) + "\n").encode()
    events_bytes = (
        "\n".join(json.dumps(item, sort_keys=True) for item in events) + "\n"
    ).encode()
    state_path.write_bytes(state_bytes)
    events_path.write_bytes(events_bytes)
    os.chmod(state_path, 0o600)
    os.chmod(events_path, 0o600)
    sealed.mkdir(mode=0o700)
    (sealed / "run_manifest.json").write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "run_id": run_id,
                "settlement_sha256": prior_settlement_sha,
            },
            sort_keys=True,
        )
        + "\n"
    )
    os.chmod(sealed / "run_manifest.json", 0o600)
    accidental_after = {
        "database": {
            "sha256": _sha256(failed_db.read_bytes()),
            "size": failed_db.stat().st_size,
        },
        "wal": {
            "sha256": _sha256(Path(f"{failed_db}-wal").read_bytes()),
            "size": Path(f"{failed_db}-wal").stat().st_size,
        },
        "shm": {
            "sha256": _sha256(Path(f"{failed_db}-shm").read_bytes()),
            "size": Path(f"{failed_db}-shm").stat().st_size,
        },
    }
    accidental_before = {
        **accidental_after,
        "database": {"sha256": "1" * 64, "size": 4096},
    }
    return {
        "source": source,
        "target": target,
        "checkpoint": checkpoint,
        "failed_db": failed_db,
        "sealed": sealed,
        "prompt": prompt,
        "gateway": gateway,
        "run_id": run_id,
        "prior_settlement_sha": prior_settlement_sha,
        "state_sha": _sha256(state_bytes),
        "events_sha": _sha256(events_bytes),
        "source_state_bytes": state_bytes,
        "source_events_bytes": events_bytes,
        "source_db_bytes": failed_db.read_bytes(),
        "source_wal_bytes": Path(f"{failed_db}-wal").read_bytes(),
        "source_shm_bytes": Path(f"{failed_db}-shm").read_bytes(),
        "sealed_bytes": (sealed / "run_manifest.json").read_bytes(),
        "prompt_sha": _sha256(prompt.read_bytes()),
        "gateway_sha": _sha256(gateway.read_bytes()),
        "accidental_before": accidental_before,
        "accidental_after": accidental_after,
    }


def provider_record(
    record_id: int,
    *,
    session_uuid: str = "session-x",
    assistant_turn_id: int = 11,
    provider: str = "deepseek",
    actual_model: str = uat.EXPECTED_MODEL,
    retry_count: int = 0,
    repair_used: bool = False,
    fallback_used: bool = False,
) -> dict[str, object]:
    return {
        "record_type": "agent_trace",
        "id": record_id,
        "session_uuid": session_uuid,
        "assistant_turn_id": assistant_turn_id,
        "model_provider": provider,
        "requested_model": uat.EXPECTED_MODEL if provider == "deepseek" else None,
        "actual_model": actual_model if provider == "deepseek" else "none",
        "transport_retry_count": retry_count,
        "repair_used": repair_used,
        "fallback_used": fallback_used,
        "renderer_status": "completed",
    }


def test_frozen_scenarios_cover_protocol_boundaries_without_model_authored_answers() -> None:
    specs = uat.scenario_specs()
    plan = uat.validate_scenarios(specs)

    assert [item.target_valid_answers for item in specs] == [40, 40, 41, 42, 44, 45]
    assert plan == {
        "scenario_count": 6,
        "planned_valid_answers": 252,
        "planned_clarifications": 2,
        "planned_logical_calls": 266,
        "theoretical_physical_max": 798,
    }
    assert len(uat.scenario_sha256(specs)) == 64
    assert uat.scenario_sha256(specs) == uat.scenario_sha256(uat.scenario_specs())

    for spec in specs:
        answers = uat.prepare_answers(spec)
        assert len(answers) == spec.target_valid_answers
        assert len({item.answer_id for item in answers}) == len(answers)
        assert len({item.text for item in answers}) == len(answers)
        assert 1 <= uat.normalized_visible_character_count(answers[0].text) < 20
        assert all(
            uat.normalized_visible_character_count(item.text) >= 20
            for item in answers[1:]
        )


def test_response_selection_is_deterministic_and_intent_sensitive() -> None:
    answers = uat.prepare_answers(uat.scenario_specs()[0])
    selector_a = uat.ResponseSelector(answers)
    selector_b = uat.ResponseSelector(answers)
    questions = (
        "先从事实说起，具体发生了什么？",
        "你有哪些记录或证据可以核实？",
        "这会影响谁，成员和同事分别承担哪些后果？",
        "如果出现反例，你会怎样修正？",
    )

    selected_a = [selector_a.select(item) for item in questions]
    selected_b = [selector_b.select(item) for item in questions]
    assert selected_a == selected_b
    assert selected_a[0].answer_id.endswith("A01")
    assert [item.intent for item in selected_a[1:]] == [
        "evidence",
        "stakeholder",
        "uncertainty",
    ]


def test_clarifications_are_scheduled_outside_valid_answer_bank() -> None:
    specs = uat.scenario_specs()
    scheduled = [
        (spec.case_id, item.after_valid_answer, item.text)
        for spec in specs
        for item in spec.clarifications
    ]
    assert [(case_id, after) for case_id, after, _ in scheduled] == [
        ("UAT-02", 10),
        ("UAT-05", 23),
    ]
    answer_texts = {
        answer.text for spec in specs for answer in uat.prepare_answers(spec)
    }
    assert all(text not in answer_texts for _, _, text in scheduled)
    assert all(
        uat.is_bounded_clarification_request(text)
        for _, _, text in scheduled
    )


def test_visible_character_count_matches_protocol_letters_and_numbers_only() -> None:
    assert uat.normalized_visible_character_count("ＡＢ１２测试") == 6
    assert uat.normalized_visible_character_count("！？…—😊 ") == 0
    assert uat.normalized_visible_character_count("测试😊！") == 2


def test_private_quality_review_retains_exact_interviewer_text() -> None:
    row = uat._quality_row(
        "UAT-X",
        "session-x",
        7,
        {
            "turn_index": 14,
            "content": "我先核对一下你刚才的意思，你最在意的依据是什么？",
            "quality_flags": ["single_question"],
        },
    )
    assert row["interviewer_text"] == "我先核对一下你刚才的意思，你最在意的依据是什么？"
    assert row["turn_index"] == 14
    assert len(str(row["message_sha256"])) == 64


def test_budget_deduplicates_database_records_and_counts_retry_and_repair() -> None:
    ledger = uat.BudgetLedger()
    records = [
        provider_record(1),
        provider_record(2, retry_count=1),
        provider_record(3, repair_used=True),
    ]

    ledger.observe(records)
    ledger.observe(records)
    assert ledger.observed_physical_attempts == 5


def test_budget_reserves_worst_case_before_each_billable_call() -> None:
    ledger = uat.BudgetLedger(hard_limit=6)
    ledger.ensure_can_call("first")
    ledger.ensure_can_call("second")
    assert ledger.reserved_physical_upper_bound == 6
    with pytest.raises(AssertionError, match="hard_request_budget_would_be_exceeded"):
        ledger.ensure_can_call("third")


def test_budget_pause_requires_separate_confirmation() -> None:
    paused = uat.BudgetLedger(soft_limit=1, pause_limit=2, hard_limit=960)
    with pytest.raises(AssertionError, match="request_budget_paused_at_600"):
        paused.observe([provider_record(1, retry_count=1)])

    approved = uat.BudgetLedger(
        soft_limit=1,
        pause_limit=2,
        hard_limit=960,
        allow_after_pause=True,
    )
    approved.observe([provider_record(1, retry_count=1)])
    approved.ensure_can_call("approved")
    assert approved.reserved_physical_upper_bound == 3


def test_exact_target_gate_rejects_early_or_wrong_origin() -> None:
    forty = uat.scenario_specs()[0]
    forty_five = uat.scenario_specs()[-1]

    uat.assert_exact_target_coverage(
        forty,
        actual_answers=40,
        close_origin="model",
    )
    uat.assert_exact_target_coverage(
        forty,
        actual_answers=40,
        close_origin="uat_finalize",
    )
    uat.assert_exact_target_coverage(
        forty_five,
        actual_answers=45,
        close_origin="technical_limit",
    )
    with pytest.raises(AssertionError, match="scenario_target_not_reached_exactly"):
        uat.assert_exact_target_coverage(
            forty_five,
            actual_answers=40,
            close_origin="model",
        )
    with pytest.raises(
        AssertionError, match="maximum_target_did_not_use_technical_limit"
    ):
        uat.assert_exact_target_coverage(
            forty_five,
            actual_answers=45,
            close_origin="model",
        )
    with pytest.raises(AssertionError, match="technical_limit_before_maximum"):
        uat.assert_exact_target_coverage(
            forty,
            actual_answers=40,
            close_origin="technical_limit",
        )
    with pytest.raises(AssertionError, match="unsupported_uat_close_origin"):
        uat.assert_exact_target_coverage(
            forty,
            actual_answers=40,
            close_origin="protocol_gate",
        )


def test_run_flow_fails_fast_when_model_closes_before_scenario_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = uat.scenario_specs()[-1]
    initial_session = {
        "uuid": "session-x",
        "phase": "interviewing",
        "valid_answer_count": 39,
        "user_answer_count": 39,
    }
    closed_session = {
        **initial_session,
        "phase": "finalizing",
        "valid_answer_count": 40,
        "user_answer_count": 40,
    }
    completed = {
        "session_action": "finish",
        "finish_reason": "natural_closure",
        "turn": {
            "id": 11,
            "turn_index": 80,
            "content": "谢谢你讲得这么具体，我们先在这里收束。",
            "quality_flags": [],
        },
    }

    monkeypatch.setattr(
        uat,
        "create_session",
        lambda *_args, **_kwargs: (initial_session, "请继续说说你的依据？", 1.0),
    )
    monkeypatch.setattr(
        uat,
        "submit_interaction",
        lambda *_args, **_kwargs: (closed_session, completed, 1.0, 1),
    )
    monkeypatch.setattr(
        uat,
        "_refresh_audit",
        lambda *_args, **_kwargs: [provider_record(1)],
    )

    with pytest.raises(AssertionError, match="scenario_closed_before_target"):
        uat.run_flow(
            object(),
            spec,
            run_id="run-x",
            audit_db=object(),
            budget=uat.BudgetLedger(),
            known_session_uuids=[],
            turn_metrics=[],
            quality_rows=[],
            output_dir=tmp_path,
        )


def test_close_origin_uses_linked_provider_and_technical_flag_not_finish_reason() -> None:
    model_completed = {
        "session_action": "finish",
        "finish_reason": "natural_closure",
        "turn": {"id": 11, "quality_flags": []},
    }
    assert uat.classify_close_origin(
        model_completed,
        [provider_record(1)],
        session_uuid="session-x",
    ) == ("model", "deepseek")

    # Even an ambiguous/public finish reason cannot misclassify the deterministic
    # cap: origin is established by the exact turn's persisted provider + flag.
    technical_completed = {
        "session_action": "finish",
        "finish_reason": "natural_closure",
        "turn": {
            "id": 12,
            "quality_flags": ["technical_maximum_reached"],
        },
    }
    technical_record = provider_record(
        2,
        assistant_turn_id=12,
        provider="protocol_gate",
    )
    assert uat.classify_close_origin(
        technical_completed,
        [technical_record],
        session_uuid="session-x",
    ) == ("technical_limit", "protocol_gate")

    continuing = {
        "session_action": "continue",
        "finish_reason": None,
        "turn": {"id": 13, "quality_flags": []},
    }
    assert uat.classify_close_origin(
        continuing,
        [],
        session_uuid="session-x",
    ) == (None, None)


def test_persistent_budget_hard_limit_survives_new_run(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "budget-ledger"
    first = uat.BudgetLedger(
        hard_limit=9,
        ledger_dir=ledger_dir,
        run_id="run-1",
        record_namespace="db-1",
    )
    first.ensure_can_call("first")
    first.ensure_can_call("second")
    first.close("FAILED")

    second = uat.BudgetLedger(
        hard_limit=9,
        ledger_dir=ledger_dir,
        run_id="run-2",
        record_namespace="db-2",
    )
    assert second.reserved_physical_upper_bound == 6
    second.ensure_can_call("third")
    with pytest.raises(AssertionError, match="hard_request_budget_would_be_exceeded"):
        second.ensure_can_call("fourth")
    second.close("BLOCKED")

    assert ledger_dir.stat().st_mode & 0o777 == 0o700
    assert (ledger_dir / "budget_state.json").stat().st_mode & 0o777 == 0o600
    assert (ledger_dir / "budget_events.jsonl").stat().st_mode & 0o777 == 0o600
    events = (ledger_dir / "budget_events.jsonl").read_text(encoding="utf-8")
    assert '"event": "run_opened"' in events
    assert '"event": "hard_limit_blocked"' in events


def test_persistent_pause_resume_does_not_reset_observed_total(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "pause-ledger"
    first = uat.BudgetLedger(
        soft_limit=1,
        pause_limit=2,
        hard_limit=960,
        allow_after_pause=True,
        ledger_dir=ledger_dir,
        run_id="run-1",
        record_namespace="db-1",
    )
    first.observe([provider_record(1, retry_count=1)])
    assert first.observed_physical_attempts == 2
    first.close("PAUSED")

    blocked = uat.BudgetLedger(
        soft_limit=1,
        pause_limit=2,
        hard_limit=960,
        ledger_dir=ledger_dir,
        run_id="run-2",
        record_namespace="db-2",
    )
    assert blocked.observed_physical_attempts == 2
    with pytest.raises(AssertionError, match="request_budget_paused_at_600"):
        blocked.ensure_can_call("blocked-after-resume")
    blocked.close("BLOCKED")

    approved = uat.BudgetLedger(
        soft_limit=1,
        pause_limit=2,
        hard_limit=960,
        allow_after_pause=True,
        ledger_dir=ledger_dir,
        run_id="run-3",
        record_namespace="db-3",
    )
    approved.ensure_can_call("approved-after-review")
    assert approved.observed_physical_attempts == 2
    assert approved.reserved_physical_upper_bound == 3
    approved.close("CONTINUED")


def test_persistent_budget_record_dedupe_is_database_namespaced(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "namespace-ledger"
    first = uat.BudgetLedger(
        ledger_dir=ledger_dir,
        run_id="run-1",
        record_namespace="db-a",
    )
    first.observe([provider_record(1)])
    first.observe([provider_record(1)])
    assert first.observed_physical_attempts == 1
    first.close("DONE")

    second = uat.BudgetLedger(
        ledger_dir=ledger_dir,
        run_id="run-2",
        record_namespace="db-b",
    )
    second.observe([provider_record(1)])
    assert second.observed_physical_attempts == 2
    second.close("DONE")


def test_persistent_budget_preflight_refuses_incomplete_remaining_capacity(
    tmp_path: Path,
) -> None:
    ledger = uat.BudgetLedger(
        hard_limit=12,
        ledger_dir=tmp_path / "capacity-ledger",
        run_id="run-1",
        record_namespace="db-1",
    )
    ledger.ensure_can_call("prior-run-call")
    with pytest.raises(
        AssertionError, match="cumulative_budget_cannot_fit_complete_run"
    ):
        ledger.ensure_run_capacity(12)
    ledger.close("BLOCKED")


def test_model_identity_is_a_hard_stop() -> None:
    uat.audit_model_identity([provider_record(1)])
    with pytest.raises(AssertionError, match="actual_model_mismatch"):
        uat.audit_model_identity([provider_record(1, actual_model="deepseek-other")])
    with pytest.raises(AssertionError, match="fallback_used_in_real_uat"):
        uat.audit_model_identity([provider_record(1, fallback_used=True)])
    with pytest.raises(AssertionError, match="no_real_model_provenance"):
        uat.audit_model_identity([])


def test_local_api_and_private_output_guards(tmp_path: Path) -> None:
    assert uat._ensure_local_base_url("http://127.0.0.1:8060/api/v1/") == (
        "http://127.0.0.1:8060/api/v1"
    )
    with pytest.raises(AssertionError, match="requires_local_api"):
        uat._ensure_local_base_url("https://example.com/api/v1")
    with pytest.raises(AssertionError, match="outside_repository"):
        uat._ensure_private_output_dir(uat.REPO_ROOT / "forbidden-uat-output")

    private_dir = uat._ensure_private_output_dir(tmp_path / "sealed-uat")
    assert private_dir.stat().st_mode & 0o777 == 0o700
    uat._write_json(private_dir / "proof.json", {"ok": True})
    assert (private_dir / "proof.json").stat().st_mode & 0o777 == 0o600
    transcript = private_dir / "transcripts" / "UAT-X.json"
    uat._write_json(
        transcript,
        {"turns": [{"role": "assistant", "content": "合成访谈文本"}]},
    )
    assert transcript.parent.stat().st_mode & 0o777 == 0o700
    assert transcript.stat().st_mode & 0o777 == 0o600


def test_missing_confirmation_exits_before_output_or_network(tmp_path: Path) -> None:
    output_dir = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit, match="Refusing billable real-model UAT"):
        uat.main(
            [
                "--output-dir",
                str(output_dir),
                "--db-path",
                str(tmp_path / "missing.db"),
            ]
        )
    assert not output_dir.exists()


def test_budget_settlement_creates_v2_without_mutating_v1(tmp_path: Path) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    result = uat.settle_budget_ledger_v1_to_v2(
        source_ledger_dir=fixture["source"],
        target_ledger_dir=fixture["target"],
        database_paths=fixture["databases"],
        expected_source_state_sha256=fixture["state_sha"],
        expected_source_events_sha256=fixture["events_sha"],
        confirmation=uat.SETTLEMENT_CONFIRMATION,
    )
    source = fixture["source"]
    target = fixture["target"]
    assert (source / "budget_state.json").read_bytes() == fixture["state_bytes"]
    assert (source / "budget_events.jsonl").read_bytes() == fixture["event_bytes"]
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())
    target_state = json.loads((target / "budget_state.json").read_text())
    assert target_state["version"] == 2
    assert target_state["reserved_physical_upper_bound"] == 75
    assert target_state["observed_physical_attempts"] == 75
    assert target_state["settlement"]["source_reserved_physical_upper_bound"] == 213
    assert target_state["settlement"]["unresolved_reservation_count"] == 0
    assert result["settlement_sha256"] == target_state["settlement"]["settlement_sha256"]

    ledger = uat.BudgetLedger(
        ledger_dir=target,
        run_id="new-run",
        record_namespace="fresh-database-namespace",
        expected_settlement_sha256=str(result["settlement_sha256"]),
    )
    ledger.ensure_run_capacity(798)
    assert ledger.reserved_physical_upper_bound + 798 == 873
    ledger.close("PREFLIGHT_ONLY")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("confirmation", "WRONG", "settlement_confirmation_mismatch"),
        ("state_sha", "0" * 64, "settlement_source_state_sha256_mismatch"),
        ("events_sha", "0" * 64, "settlement_source_events_sha256_mismatch"),
    ],
)
def test_budget_settlement_rejects_confirmation_and_source_hash_changes(
    tmp_path: Path,
    field: str,
    value: str,
    match: str,
) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    arguments = {
        "source_ledger_dir": fixture["source"],
        "target_ledger_dir": fixture["target"],
        "database_paths": fixture["databases"],
        "expected_source_state_sha256": fixture["state_sha"],
        "expected_source_events_sha256": fixture["events_sha"],
        "confirmation": uat.SETTLEMENT_CONFIRMATION,
    }
    if field == "state_sha":
        arguments["expected_source_state_sha256"] = value
    elif field == "events_sha":
        arguments["expected_source_events_sha256"] = value
    else:
        arguments["confirmation"] = value
    with pytest.raises(AssertionError, match=match):
        uat.settle_budget_ledger_v1_to_v2(**arguments)
    assert not fixture["target"].exists()


def test_budget_settlement_rejects_existing_or_repository_target(tmp_path: Path) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    target = fixture["target"]
    target.mkdir()
    common = {
        "source_ledger_dir": fixture["source"],
        "database_paths": fixture["databases"],
        "expected_source_state_sha256": fixture["state_sha"],
        "expected_source_events_sha256": fixture["events_sha"],
        "confirmation": uat.SETTLEMENT_CONFIRMATION,
    }
    with pytest.raises(AssertionError, match="target_ledger_must_not_exist"):
        uat.settle_budget_ledger_v1_to_v2(target_ledger_dir=target, **common)
    with pytest.raises(AssertionError, match="target_ledger_must_be_outside"):
        uat.settle_budget_ledger_v1_to_v2(
            target_ledger_dir=uat.REPO_ROOT / "must-not-create-v2-ledger",
            **common,
        )


def test_budget_settlement_zero_observed_reservation_and_unclosed_run_gate(
    tmp_path: Path,
) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    state = json.loads(fixture["state_bytes"])
    events = [json.loads(line) for line in fixture["event_bytes"].splitlines()]
    # The first reservation legitimately has observed=0. It must not be treated
    # as a missing value by the settlement gate.
    reservations = uat._verify_settlement_source_events(state, events)
    assert reservations[0]["audit_delta"] == 2
    with pytest.raises(AssertionError, match="settlement_unclosed_run"):
        uat._verify_settlement_source_events(state, events[:-1])


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            "missing",
            "missing_provider_record|record_keys_mismatch|provider_attempt_mismatch",
        ),
        ("model", "requested_model_mismatch"),
        ("fallback", "fallback_used_in_real_uat"),
        ("response_missing", "settlement_response_id_missing"),
        ("response_duplicate", "settlement_duplicate_response_id"),
    ],
)
def test_budget_settlement_rejects_bad_database_provenance(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    database_path = fixture["databases"][0]
    connection = sqlite3.connect(database_path)
    if mutation == "missing":
        connection.execute("DELETE FROM agent_traces WHERE id = 1")
    elif mutation == "model":
        connection.execute(
            "UPDATE agent_traces SET requested_model='wrong-model' WHERE id=1"
        )
    elif mutation == "fallback":
        connection.execute("UPDATE agent_traces SET fallback_used=1 WHERE id=1")
    elif mutation == "response_missing":
        connection.execute("UPDATE agent_traces SET response_id=NULL WHERE id=1")
    else:
        connection.execute(
            "UPDATE agent_traces SET response_id='response-2' WHERE id=1"
        )
    connection.commit()
    connection.close()
    with pytest.raises(AssertionError, match=match):
        uat.settle_budget_ledger_v1_to_v2(
            source_ledger_dir=fixture["source"],
            target_ledger_dir=fixture["target"],
            database_paths=fixture["databases"],
            expected_source_state_sha256=fixture["state_sha"],
            expected_source_events_sha256=fixture["events_sha"],
            confirmation=uat.SETTLEMENT_CONFIRMATION,
        )
    assert not fixture["target"].exists()


def test_formal_uat_requires_v2_settlement_sha_before_network(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="expected_settlement_sha256"):
        uat.main(
            [
                "--confirmation",
                uat.CONFIRMATION,
                "--db-path",
                str(tmp_path / "missing.sqlite3"),
                "--output-dir",
                str(tmp_path / "output"),
                "--budget-ledger-dir",
                str(tmp_path / "ledger"),
            ]
        )


def test_budget_ledger_rejects_wrong_settlement_sha(tmp_path: Path) -> None:
    fixture = _make_settlement_fixture(tmp_path)
    result = uat.settle_budget_ledger_v1_to_v2(
        source_ledger_dir=fixture["source"],
        target_ledger_dir=fixture["target"],
        database_paths=fixture["databases"],
        expected_source_state_sha256=fixture["state_sha"],
        expected_source_events_sha256=fixture["events_sha"],
        confirmation=uat.SETTLEMENT_CONFIRMATION,
    )
    assert result["settlement_sha256"] != "0" * 64
    with pytest.raises(AssertionError, match="settlement_sha256_mismatch"):
        uat.BudgetLedger(
            ledger_dir=fixture["target"],
            run_id="blocked",
            record_namespace="fresh-database-namespace",
            expected_settlement_sha256="0" * 64,
        )


def test_aborted_disk_full_settlement_creates_forensic_v3_without_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_aborted_disk_fixture(tmp_path)
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_BEFORE", fixture["accidental_before"])
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_AFTER", fixture["accidental_after"])
    result = uat.settle_aborted_disk_full_v2_to_v3(
        source_ledger_dir=fixture["source"],
        target_ledger_dir=fixture["target"],
        source_database_path=fixture["failed_db"],
        source_sealed_dir=fixture["sealed"],
        checkpoint_archive_dir=fixture["checkpoint"],
        prompt_path=fixture["prompt"],
        gateway_path=fixture["gateway"],
        expected_source_state_sha256=fixture["state_sha"],
        expected_source_events_sha256=fixture["events_sha"],
        expected_prior_settlement_sha256=fixture["prior_settlement_sha"],
        expected_prompt_sha256=fixture["prompt_sha"],
        expected_gateway_sha256=fixture["gateway_sha"],
        expected_run_id=fixture["run_id"],
        confirmation=uat.ABORT_SETTLEMENT_CONFIRMATION,
    )
    assert (fixture["source"] / "budget_state.json").read_bytes() == fixture[
        "source_state_bytes"
    ]
    assert (fixture["source"] / "budget_events.jsonl").read_bytes() == fixture[
        "source_events_bytes"
    ]
    assert fixture["failed_db"].read_bytes() == fixture["source_db_bytes"]
    assert Path(f"{fixture['failed_db']}-wal").read_bytes() == fixture[
        "source_wal_bytes"
    ]
    assert Path(f"{fixture['failed_db']}-shm").read_bytes() == fixture[
        "source_shm_bytes"
    ]
    assert (fixture["sealed"] / "run_manifest.json").read_bytes() == fixture[
        "sealed_bytes"
    ]
    assert fixture["target"].stat().st_mode & 0o777 == 0o700
    assert fixture["checkpoint"].stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for directory in (fixture["target"], fixture["checkpoint"])
        for path in directory.iterdir()
        if path.is_file()
    )
    report_path = fixture["checkpoint"] / "forensic_report.json"
    assert result["forensic_report_sha256"] == uat._sha256_file(report_path)
    report = json.loads(report_path.read_text())
    assert report["failure_reason"] == "aborted_disk_full"
    assert report["aborted_run_id"] == fixture["run_id"]
    assert report["conservative_carry"] == 138
    assert report["logical_call_reservations"] == 21
    assert report["provider_record_count"] == 20
    assert report["observed_physical_attempts_during_aborted_run"] == 21
    assert report["accidental_checkpoint_before"] == fixture["accidental_before"]
    assert report["accidental_checkpoint_after"] == fixture["accidental_after"]
    assert report["sqlite_checkpoint"]["quick_check"] == ["ok"]
    assert report["sqlite_checkpoint"]["integrity_check"] == ["ok"]
    target_state = json.loads((fixture["target"] / "budget_state.json").read_text())
    assert target_state["version"] == 3
    assert target_state["observed_physical_attempts"] == 96
    assert target_state["reserved_physical_upper_bound"] == 138
    assert target_state["settlement"]["failure_reason"] == "aborted_disk_full"
    assert target_state["settlement"]["accidental_checkpoint_before"] == fixture[
        "accidental_before"
    ]
    assert target_state["settlement"]["accidental_checkpoint_after"] == fixture[
        "accidental_after"
    ]
    assert result["settlement_sha256"] == target_state["settlement"][
        "settlement_sha256"
    ]
    ledger = uat.BudgetLedger(
        ledger_dir=fixture["target"],
        run_id="fresh-run",
        record_namespace="fresh-database-namespace",
        expected_settlement_sha256=str(result["settlement_sha256"]),
    )
    ledger.ensure_run_capacity(798)
    assert ledger.reserved_physical_upper_bound + 798 == 936
    ledger.close("PREFLIGHT_ONLY")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("run_id", "wrong-run", "run_id_mismatch"),
        ("state_sha", "0" * 64, "source_state_sha256_mismatch"),
        ("prior_sha", "0" * 64, "prior_settlement_sha_mismatch"),
        ("prompt_sha", "0" * 64, "prompt_sha256_mismatch"),
        ("confirmation", "WRONG", "confirmation_mismatch"),
    ],
)
def test_aborted_disk_full_settlement_rejects_wrong_frozen_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    match: str,
) -> None:
    fixture = _make_aborted_disk_fixture(tmp_path)
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_BEFORE", fixture["accidental_before"])
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_AFTER", fixture["accidental_after"])
    arguments = {
        "source_ledger_dir": fixture["source"],
        "target_ledger_dir": fixture["target"],
        "source_database_path": fixture["failed_db"],
        "source_sealed_dir": fixture["sealed"],
        "checkpoint_archive_dir": fixture["checkpoint"],
        "prompt_path": fixture["prompt"],
        "gateway_path": fixture["gateway"],
        "expected_source_state_sha256": fixture["state_sha"],
        "expected_source_events_sha256": fixture["events_sha"],
        "expected_prior_settlement_sha256": fixture["prior_settlement_sha"],
        "expected_prompt_sha256": fixture["prompt_sha"],
        "expected_gateway_sha256": fixture["gateway_sha"],
        "expected_run_id": fixture["run_id"],
        "confirmation": uat.ABORT_SETTLEMENT_CONFIRMATION,
    }
    key = {
        "run_id": "expected_run_id",
        "state_sha": "expected_source_state_sha256",
        "prior_sha": "expected_prior_settlement_sha256",
        "prompt_sha": "expected_prompt_sha256",
        "confirmation": "confirmation",
    }[field]
    arguments[key] = value
    with pytest.raises(AssertionError, match=match):
        uat.settle_aborted_disk_full_v2_to_v3(**arguments)
    assert not fixture["target"].exists()
    assert not fixture["checkpoint"].exists()


def test_aborted_disk_full_settlement_rejects_nonfrozen_current_triplet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_aborted_disk_fixture(tmp_path)
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_BEFORE", fixture["accidental_before"])
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_AFTER", fixture["accidental_after"])
    wrong_after = {
        **fixture["accidental_after"],
        "wal": {"sha256": "2" * 64, "size": 0},
    }
    with pytest.raises(
        AssertionError, match="current_triplet_not_frozen_after"
    ):
        uat.settle_aborted_disk_full_v2_to_v3(
            source_ledger_dir=fixture["source"],
            target_ledger_dir=fixture["target"],
            source_database_path=fixture["failed_db"],
            source_sealed_dir=fixture["sealed"],
            checkpoint_archive_dir=fixture["checkpoint"],
            prompt_path=fixture["prompt"],
            gateway_path=fixture["gateway"],
            expected_source_state_sha256=fixture["state_sha"],
            expected_source_events_sha256=fixture["events_sha"],
            expected_prior_settlement_sha256=fixture["prior_settlement_sha"],
            expected_prompt_sha256=fixture["prompt_sha"],
            expected_gateway_sha256=fixture["gateway_sha"],
            expected_run_id=fixture["run_id"],
            confirmation=uat.ABORT_SETTLEMENT_CONFIRMATION,
            expected_accidental_checkpoint_before=fixture["accidental_before"],
            expected_accidental_checkpoint_after=wrong_after,
        )
    assert not fixture["target"].exists()
    assert not fixture["checkpoint"].exists()


def test_v3_budget_ledger_rejects_prior_v2_settlement_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_aborted_disk_fixture(tmp_path)
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_BEFORE", fixture["accidental_before"])
    monkeypatch.setattr(uat, "ACCIDENTAL_CHECKPOINT_AFTER", fixture["accidental_after"])
    result = uat.settle_aborted_disk_full_v2_to_v3(
        source_ledger_dir=fixture["source"],
        target_ledger_dir=fixture["target"],
        source_database_path=fixture["failed_db"],
        source_sealed_dir=fixture["sealed"],
        checkpoint_archive_dir=fixture["checkpoint"],
        prompt_path=fixture["prompt"],
        gateway_path=fixture["gateway"],
        expected_source_state_sha256=fixture["state_sha"],
        expected_source_events_sha256=fixture["events_sha"],
        expected_prior_settlement_sha256=fixture["prior_settlement_sha"],
        expected_prompt_sha256=fixture["prompt_sha"],
        expected_gateway_sha256=fixture["gateway_sha"],
        expected_run_id=fixture["run_id"],
        confirmation=uat.ABORT_SETTLEMENT_CONFIRMATION,
    )
    assert result["settlement_sha256"] != fixture["prior_settlement_sha"]
    with pytest.raises(AssertionError, match="settlement_sha256_mismatch"):
        uat.BudgetLedger(
            ledger_dir=fixture["target"],
            run_id="blocked",
            record_namespace="fresh-database-namespace",
            expected_settlement_sha256=str(fixture["prior_settlement_sha"]),
        )
