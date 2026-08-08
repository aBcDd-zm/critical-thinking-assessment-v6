from __future__ import annotations

import importlib
import hashlib
import json
import threading
import time
from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.models import (
    AgentTrace,
    AssessmentSession,
    EvidenceAttributionSpan,
    EvidenceItem,
    EvidenceReadinessCheck,
    TechnicalAnomaly,
    utcnow,
)
from app.schemas import (
    AttributedFinalScorerOutput,
    EvidenceAttributionOutput,
    NaturalInterviewerOutput,
)
from app.services.model_gateway import (
    ATTRIBUTED_EVIDENCE_SCHEMA_VERSION,
    ATTRIBUTED_EVIDENCE_SYSTEM_PROMPT,
    EVIDENCE_CANDIDATE_RULE_VERSION,
    EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
    EVIDENCE_ATTRIBUTION_SYSTEM_PROMPT,
    EvidenceAttributionSelectionOutput,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_0,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_1,
    ModelGatewayError,
    ModelGatewayService,
    _validate_v621_interviewer_contract,
    attach_attribution_span_candidates,
    build_attribution_span_candidates,
    build_interview_anchor_candidates,
    source_clarification_required,
)
from app.services.orchestrator import (
    FinalizationError,
    InterviewContractError,
    InterviewOrchestrator,
    _validated_navigation,
    attribution_text_hash,
    materialize_attribution_output,
    validate_attribution_output,
)
from app.services.session_service import (
    _lease_token,
    _report_readiness_asset_fingerprint,
)
from tests.conftest import (
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USERNAME,
    TestSession,
)


router_module = importlib.import_module("app.api.router")
session_service_module = importlib.import_module("app.services.session_service")


CASE_15 = (
    "因为我之前有和它聊过很多有关于这个选题的想法，根据它原有的记忆功能，"
    "我通常会省略掉很多信息，直接提问它我的问题。同时我会喂给它一些论文框架，"
    "让它进行学习并输出对应框架，我再去进行借鉴。AI给出来的信息有时会缺乏稳定性，"
    "当我开始质疑它的时候，它也会质疑自己，并修改它的内容，所以很多内容我都会进行质疑，"
    "这个质疑的过程会持续很长时间，但是每次它都会对之前的框架进行刷新，所以每次给出来的内容都是新的。"
    "这一点让我很难受  这是一次聊天时我向它描述的内容 一个由基金会孵化的公益项目，如何通过18年的组织演进，"
    "在“公益逻辑”与“商业逻辑”的张力中，找到可持续的组织管理模式？这个题目可以是什么？"
    "还有我在想要研究爱德面包坊可能还需要更多深入内部去调查，但是我们可能只能去店里面找店员。"
    "这个我们可以用什么研究方法？？感觉类似于这种访谈法会好一些"
    "上文利用多种数据来源以获得对研究对象的多视角描述，多种数据来源使研究者能对不同证据进行“三角验证”，"
    "从而提高研究信度和效度"
)


def _create_session(client) -> str:
    response = client.post(
        "/api/v1/sessions",
        json={
            "consent_version": "v6.2.1-test",
            "consent_given": True,
            "participant": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session"]["uuid"]


def _submit(client, session_uuid: str, content: str, suffix: str = "00000001"):
    response = client.post(
        f"/api/v1/sessions/{session_uuid}/turns:stream",
        json={
            "content": content,
            "client_turn_id": f"attr-{suffix}",
            "input_mode": "text",
            "answer_duration_ms": 1000,
        },
    )
    assert response.status_code == 200, response.text
    return response


def _configure_v621(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(settings, "natural_interviewer_prompt_version", "v6.2.1")
    monkeypatch.setattr(settings, "evidence_attribution_mode", mode)


def _attribution_payload(user_turns: list[dict]) -> dict:
    return {"user_turns": attach_attribution_span_candidates(user_turns)}


def _interview_payload(transcript: list[dict]) -> dict:
    return {
        "participant": {},
        "transcript": transcript,
        "anchor_candidates": build_interview_anchor_candidates(transcript),
        "source_clarification_required": source_clarification_required(
            transcript
        ),
    }


def _span_candidates(payload: dict) -> list[dict]:
    return [
        candidate
        for turn in payload["user_turns"]
        for candidate in turn["span_candidates"]
    ]


def _materialized_mock_output(payload: dict) -> EvidenceAttributionOutput:
    selection = ModelGatewayService().generate_evidence_attribution(payload).output
    return materialize_attribution_output(selection, _span_candidates(payload))


def test_case_15_separates_owned_reasoning_ai_questions_and_external_text() -> None:
    payload = _attribution_payload(
        [
            {
                "turn_index": 15,
                "content": CASE_15,
                "preceding_question": "你当时怎么判断？",
            }
        ]
    )
    candidates = _span_candidates(payload)
    assert len(candidates) == 3
    assert "".join(candidate["quote"] for candidate in candidates) == CASE_15
    assert candidates[1]["quote"].startswith(
        "这是一次聊天时我向它描述的内容"
    )
    assert candidates[2]["quote"].startswith("上文利用")
    previous_end = 0
    for candidate in candidates:
        assert candidate["start"] == previous_end
        assert CASE_15[candidate["start"] : candidate["end"]] == candidate["quote"]
        previous_end = candidate["end"]
    assert previous_end == len(CASE_15)

    output = _materialized_mock_output(payload)
    validated = validate_attribution_output(
        output,
        [{"turn_index": 15, "role": "user", "content": CASE_15}],
        span_candidates=candidates,
    )
    assert [item.eligibility for item in validated] == [
        "eligible",
        "context_only",
        "context_only",
    ]
    assert validated[1].output.relation == "asks_or_requests"
    assert validated[2].output.owner == "external_quoted"


def test_span_validation_uses_offsets_not_first_substring_and_rejects_role_hash() -> None:
    text = "重复证据，然后再出现重复证据。"
    second_start = text.rindex("重复证据")
    raw = {
        "spans": [
            {
                "turn_index": 1,
                "quote": "重复证据",
                "start": second_start,
                "end": second_start + 4,
                "text_hash": None,
                "owner": "participant_owned",
                "relation": "critiques",
                "elicitation_level": "open_probe",
                "source_label": None,
                "confidence": 0.9,
                "reason": "参与者自己的批评。",
            }
        ]
    }
    output = EvidenceAttributionOutput.model_validate(raw)
    validated = validate_attribution_output(
        output, [{"turn_index": 1, "role": "user", "content": text}]
    )
    assert validated[0].output.start == second_start
    assert validated[0].text_hash == attribution_text_hash("重复证据")

    wrong_hash = {
        **raw,
        "spans": [{**raw["spans"][0], "text_hash": "0" * 64}],
    }
    with pytest.raises(FinalizationError, match="text_hash_mismatch"):
        validate_attribution_output(
            EvidenceAttributionOutput.model_validate(wrong_hash),
            [{"turn_index": 1, "role": "user", "content": text}],
        )
    with pytest.raises(FinalizationError, match="must_reference_user_turn"):
        validate_attribution_output(
            output,
            [{"turn_index": 1, "role": "assistant", "content": text}],
        )


def test_attribution_requires_coverage_for_every_nonempty_user_turn() -> None:
    first = "我会先核实数据来源。"
    second = "AI和我的说法已经混在一起，现在分不清。"
    output = EvidenceAttributionOutput.model_validate(
        {
            "spans": [
                _span_raw(
                    turn_index=1,
                    quote=first,
                    owner="participant_owned",
                    relation="own_reasoning",
                )
            ]
        }
    )
    with pytest.raises(FinalizationError, match="attribution_output_missing_user_turn"):
        validate_attribution_output(
            output,
            [
                {"turn_index": 1, "role": "user", "content": first},
                {"turn_index": 2, "role": "assistant", "content": "那后来呢？"},
                {"turn_index": 3, "role": "user", "content": second},
                {"turn_index": 4, "role": "user", "content": "   "},
            ],
        )


def test_mock_uses_whole_turn_uncertain_when_a_turn_cannot_be_split() -> None:
    content = "AI和我的说法已经混在一起，我不确定是谁说的。"
    payload = _attribution_payload(
        [
            {
                "turn_index": 5,
                "content": content,
                "preceding_question": "哪些是你自己的判断？",
            }
        ]
    )
    selection = ModelGatewayService().generate_evidence_attribution(payload).output
    assert selection.spans[0].candidate_id == _span_candidates(payload)[0]["candidate_id"]
    assert "quote" not in selection.spans[0].model_dump(mode="json")
    output = materialize_attribution_output(selection, _span_candidates(payload))
    assert len(output.spans) == 1
    assert output.spans[0].quote == content
    assert output.spans[0].start == 0
    assert output.spans[0].end == len(content)
    assert output.spans[0].owner == "uncertain"
    validated = validate_attribution_output(
        output, [{"turn_index": 5, "role": "user", "content": content}]
    )
    assert validated[0].eligibility == "manual_review"
    assert "每个 span_candidate 必须恰好输出一次" in EVIDENCE_ATTRIBUTION_SYSTEM_PROMPT


def test_span_candidates_preserve_repeated_occurrences_and_reject_substitution() -> None:
    content = "AI建议核实，但我不同意。AI建议核实，但我仍然不同意。"
    payload = _attribution_payload(
        [
            {
                "turn_index": 7,
                "content": content,
                "preceding_question": "你如何判断？",
            }
        ]
    )
    candidates = _span_candidates(payload)
    repeated = [candidate for candidate in candidates if candidate["quote"] == "AI建议核实，"]
    assert len(repeated) == 2
    assert repeated[0]["start"] != repeated[1]["start"]
    assert [candidate["occurrence"] for candidate in repeated] == [1, 2]
    assert repeated[0]["quote_hash"] == repeated[1]["quote_hash"]
    assert repeated[0]["candidate_id"] != repeated[1]["candidate_id"]
    assert "".join(candidate["quote"] for candidate in candidates) == content

    output = _materialized_mock_output(payload)
    validate_attribution_output(
        output,
        [{"turn_index": 7, "role": "user", "content": content}],
        span_candidates=candidates,
    )

    raw = output.model_dump(mode="json")
    first_start = repeated[0]["start"]
    first_end = repeated[0]["end"]
    corrupted_spans = []
    removed_first = False
    replaced_second = False
    for span in raw["spans"]:
        if (
            span["quote"] == "AI建议核实，"
            and span["start"] == first_start
            and not removed_first
        ):
            removed_first = True
            continue
        if span["quote"] == "AI建议核实，" and not replaced_second:
            span = {**span, "start": first_start, "end": first_end}
            replaced_second = True
        corrupted_spans.append(span)
    corrupted = EvidenceAttributionOutput.model_validate({"spans": corrupted_spans})
    with pytest.raises(FinalizationError, match="missing_span_candidate"):
        validate_attribution_output(
            corrupted,
            [{"turn_index": 7, "role": "user", "content": content}],
            span_candidates=candidates,
        )


def test_attribution_gateway_requires_canonical_server_span_candidates() -> None:
    turn = {
        "turn_index": 1,
        "content": "AI建议立刻上线，但我不同意。",
        "preceding_question": "你如何判断？",
    }
    gateway = ModelGatewayService()
    with pytest.raises(ModelGatewayError, match="invalid_attribution_span_candidates"):
        gateway.generate_evidence_attribution({"user_turns": [turn]})
    payload = _attribution_payload([turn])
    payload["user_turns"][0]["span_candidates"][0] = {
        **payload["user_turns"][0]["span_candidates"][0],
        "start": 1,
    }
    with pytest.raises(ModelGatewayError, match="invalid_attribution_span_candidates"):
        gateway.generate_evidence_attribution(payload)


def test_compact_selection_keeps_ten_turn_12000_character_input_out_of_output() -> None:
    user_turns = [
        {
            "turn_index": index * 2 + 1,
            "content": "甲" * 12000 if index == 0 else f"第{index + 1}轮我会核实证据。",
            "preceding_question": "你当时怎么判断？",
        }
        for index in range(10)
    ]
    payload = _attribution_payload(user_turns)
    candidates = _span_candidates(payload)
    assert len(candidates) == 10
    assert len(candidates[0]["quote"]) == 12000

    selection = ModelGatewayService().generate_evidence_attribution(payload).output
    serialized = selection.model_dump_json()
    assert len(selection.spans) == 10
    # Ten classifications stay comfortably below the 2,000-token evidence
    # profile even when one source turn reaches the 12,000-character limit.
    assert len(serialized.encode("utf-8")) < 4000
    assert "甲" * 100 not in serialized
    for item in selection.spans:
        assert set(item.model_dump(mode="json")) == {
            "candidate_id",
            "owner",
            "relation",
            "elicitation_level",
            "source_label",
            "confidence",
            "reason",
        }
        assert item.candidate_id.startswith("span_")
        assert len(item.candidate_id) == 69

    materialized = materialize_attribution_output(selection, candidates)
    assert materialized.spans[0].quote == "甲" * 12000
    validate_attribution_output(
        materialized,
        [
            {
                "turn_index": row["turn_index"],
                "role": "user",
                "content": row["content"],
            }
            for row in user_turns
        ],
        span_candidates=candidates,
    )


def test_candidate_id_materialization_rejects_missing_duplicate_unknown_and_tamper() -> None:
    content = "AI建议立即上线，但我不同意。"
    payload = _attribution_payload(
        [
            {
                "turn_index": 1,
                "content": content,
                "preceding_question": "你如何判断？",
            }
        ]
    )
    candidates = _span_candidates(payload)
    selection = ModelGatewayService().generate_evidence_attribution(payload).output
    raw = selection.model_dump(mode="json")
    assert len(raw["spans"]) == 2

    missing = EvidenceAttributionSelectionOutput.model_validate(
        {"spans": raw["spans"][:1]}
    )
    with pytest.raises(FinalizationError, match="missing_candidate_id"):
        materialize_attribution_output(missing, candidates)

    duplicate = EvidenceAttributionSelectionOutput.model_validate(
        {"spans": [*raw["spans"], raw["spans"][0]]}
    )
    with pytest.raises(FinalizationError, match="duplicate_candidate_id"):
        materialize_attribution_output(duplicate, candidates)

    unknown_raw = json.loads(json.dumps(raw, ensure_ascii=False))
    unknown_raw["spans"][0]["candidate_id"] = "span_" + "0" * 64
    unknown = EvidenceAttributionSelectionOutput.model_validate(unknown_raw)
    with pytest.raises(FinalizationError, match="unknown_candidate_id"):
        materialize_attribution_output(unknown, candidates)

    tampered_registry = [dict(candidate) for candidate in candidates]
    tampered_registry[0]["start"] += 1
    with pytest.raises(FinalizationError, match="registry_identity_mismatch"):
        materialize_attribution_output(selection, tampered_registry)


def test_service_forces_explicit_whole_turn_mixture_to_manual_review() -> None:
    content = "AI和我的说法混在一起，现在分不清来源。"
    payload = _attribution_payload(
        [
            {
                "turn_index": 3,
                "content": content,
                "preceding_question": "哪些是你自己的判断？",
            }
        ]
    )
    candidates = _span_candidates(payload)
    assert candidates[0]["force_uncertain"] is True
    malicious = EvidenceAttributionSelectionOutput.model_validate(
        {
            "spans": [
                {
                    "candidate_id": candidates[0]["candidate_id"],
                    "owner": "participant_owned",
                    "relation": "own_reasoning",
                    "elicitation_level": "open_probe",
                    "source_label": None,
                    "confidence": 1.0,
                    "reason": "试图将混合文本当作本人推理。",
                }
            ]
        }
    )
    materialized = materialize_attribution_output(malicious, candidates)
    assert materialized.spans[0].owner == "uncertain"
    assert materialized.spans[0].relation == "quotes_only"
    validated = validate_attribution_output(
        materialized,
        [{"turn_index": 3, "role": "user", "content": content}],
        span_candidates=candidates,
    )
    assert validated[0].eligibility == "manual_review"


def test_attribution_candidate_limit_fails_closed_but_anchor_stays_bounded() -> None:
    content = "外部观点，" + "但我认为需要核实，" * 100
    turns = [{"turn_index": 1, "content": content}]
    with pytest.raises(ValueError, match="attribution_candidate_limit_exceeded"):
        build_attribution_span_candidates(turns)
    anchors = build_interview_anchor_candidates(
        [{"turn_index": 1, "role": "user", "content": content}]
    )
    assert len(anchors) == 100
    assert all(set(anchor) == {"turn_index", "quote", "start", "end"} for anchor in anchors)


def test_enforce_uses_attributed_ids_and_never_calls_raw_scorer(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    gateway = router_module.sessions.orchestrator.gateway
    monkeypatch.setattr(
        gateway,
        "generate_incremental_evidence",
        lambda _payload: (_ for _ in ()).throw(AssertionError("raw scorer called")),
    )
    monkeypatch.setattr(
        gateway,
        "generate_final_scorer",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("finalize made a second model call")
        ),
    )
    session_uuid = _create_session(client)
    _submit(
        client,
        session_uuid,
        "AI说应该直接上线。但我认为要先核实数据来源，权衡风险后再决定方案。",
    )

    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        assert session is not None and session.phase == "interviewing"
        check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == session.id
            )
        )
        assert check is not None and check.status == "insufficient"
        assert check.prompt_version == "v6.2.2"
        assert "evidence_refs" in check.result_data["dimensions"][0]
        spans = list(
            db.scalars(
                select(EvidenceAttributionSpan).where(
                    EvidenceAttributionSpan.readiness_check_id == check.id
                )
            )
        )
        assert {span.eligibility for span in spans} == {"context_only", "eligible"}
        assert all(
            span.schema_version == EVIDENCE_ATTRIBUTION_SCHEMA_VERSION
            for span in spans
        )
        check_id = check.id
        fingerprint = check.transcript_fingerprint

    login = client.post(
        "/api/v1/admin/auth/login",
        json={
            "username": TEST_ADMIN_USERNAME,
            "password": TEST_ADMIN_PASSWORD,
        },
    )
    assert login.status_code == 200, login.text
    pre_final_detail = client.get(f"/api/v1/admin/sessions/{session_uuid}")
    assert pre_final_detail.status_code == 200, pre_final_detail.text
    pre_final_attributions = pre_final_detail.json()["evidence_attributions"]
    assert any(
        item["snapshot_used_dimension_keys"] for item in pre_final_attributions
    )
    assert all(
        item["used_dimension_keys"] == item["final_scoring_dimension_keys"] == []
        for item in pre_final_attributions
    )

    finalized = client.post(
        f"/api/v1/sessions/{session_uuid}/finalize",
        json={
            "evidence_check_id": check_id,
            "expected_transcript_fingerprint": fingerprint,
            "allow_incomplete": True,
        },
    )
    assert finalized.status_code == 200, finalized.text
    detail = client.get(f"/api/v1/admin/sessions/{session_uuid}")
    assert detail.status_code == 200, detail.text
    attributions = detail.json()["evidence_attributions"]
    assert attributions
    assert all("eliciting_question" in item for item in attributions)
    assert any(item["used_dimension_keys"] for item in attributions)
    assert all(
        item["used_dimension_keys"] == item["final_scoring_dimension_keys"]
        for item in attributions
    )
    assert all(
        item["source_type"] == "user"
        and item["status"] == "sufficient"
        and item["active_for_scoring"] is True
        for item in detail.json()["evidence_items"]
    )
    with TestSession() as db:
        evidence = list(db.scalars(select(EvidenceItem)))
        assert evidence
        assert all(item.attribution_span_id is not None for item in evidence)
        assert all(item.validation_status == "validated" for item in evidence)


def test_enforce_attribution_failure_does_not_freeze_or_fallback(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    gateway = router_module.sessions.orchestrator.gateway
    raw_called = False

    def fail_attribution(_payload):
        raise ModelGatewayError("attribution_failed")

    def raw_scorer(_payload):
        nonlocal raw_called
        raw_called = True
        raise AssertionError("enforce must not fall back")

    monkeypatch.setattr(gateway, "generate_evidence_attribution", fail_attribution)
    monkeypatch.setattr(gateway, "generate_incremental_evidence", raw_scorer)
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")

    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        check = db.scalar(select(EvidenceReadinessCheck))
        assert session is not None and session.phase == "interviewing"
        assert check is not None and check.status == "failed"
        assert raw_called is False
        failed_traces = list(
            db.scalars(
                select(AgentTrace).where(AgentTrace.renderer_status == "failed")
            )
        )
        assert any("evidence_attribution" in trace.action for trace in failed_traces)


def test_saved_user_turn_schedules_snapshot_after_interviewer_failure(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    session_uuid = _create_session(client)
    gateway = router_module.sessions.orchestrator.gateway
    scheduled = threading.Event()
    original_schedule = router_module.sessions.schedule_evidence_snapshot

    def record_schedule(session_factory, scheduled_uuid, *, retry_failed=False):
        try:
            return original_schedule(
                session_factory,
                scheduled_uuid,
                retry_failed=retry_failed,
            )
        finally:
            scheduled.set()

    def fail_interviewer(_payload, *, prompt_version=None):
        raise ModelGatewayError("interviewer_failed_after_user_save")

    monkeypatch.setattr(
        router_module.sessions, "schedule_evidence_snapshot", record_schedule
    )
    monkeypatch.setattr(gateway, "generate_interviewer", fail_interviewer)
    response = _submit(
        client,
        session_uuid,
        "我会先核实数据来源，再根据反例调整决定。",
        suffix="00000031",
    )
    events = [
        json.loads(line) for line in response.text.splitlines() if line.strip()
    ]
    assert events[-1]["code"] == "turn_processing_failed"
    assert scheduled.wait(timeout=2)
    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        assert session is not None and session.phase == "interviewing"
        check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == session.id
            )
        )
        assert check is not None and check.status in {"ready", "insufficient"}


def test_snapshot_schedule_error_does_not_replace_original_interviewer_error(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    session_uuid = _create_session(client)
    scheduled = threading.Event()

    def fail_schedule(_session_factory, _session_uuid, *, retry_failed=False):
        scheduled.set()
        raise RuntimeError("synthetic_snapshot_schedule_failure")

    monkeypatch.setattr(
        router_module.sessions, "schedule_evidence_snapshot", fail_schedule
    )
    monkeypatch.setattr(
        router_module.sessions.orchestrator.gateway,
        "generate_interviewer",
        lambda _payload, *, prompt_version=None: (_ for _ in ()).throw(
            ModelGatewayError("original_interviewer_failure")
        ),
    )
    response = _submit(
        client,
        session_uuid,
        "我会先核实数据来源，再根据反例调整决定。",
        suffix="00000032",
    )
    events = [
        json.loads(line) for line in response.text.splitlines() if line.strip()
    ]
    assert scheduled.wait(timeout=2)
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "turn_processing_failed"
    assert events[-1]["data"]["error_type"] == "original_interviewer_failure"
    anomaly = None
    deadline = time.monotonic() + 2
    while anomaly is None and time.monotonic() < deadline:
        with TestSession() as db:
            anomaly = db.scalar(
                select(TechnicalAnomaly).where(
                    TechnicalAnomaly.category
                    == "evidence_snapshot_schedule_failure"
                )
            )
        if anomaly is None:
            time.sleep(0.01)
    assert anomaly is not None
    assert "synthetic_snapshot_schedule_failure" in anomaly.detail


def test_legacy_v620_session_stays_on_raw_chain_under_global_enforce(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "natural_interviewer_prompt_version", "v6.2.0")
    monkeypatch.setattr(settings, "evidence_attribution_mode", "enforce")
    gateway = router_module.sessions.orchestrator.gateway
    monkeypatch.setattr(
        gateway,
        "generate_evidence_attribution",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("legacy session was switched to attribution")
        ),
    )
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    with TestSession() as db:
        check = db.scalar(select(EvidenceReadinessCheck))
        assert check is not None and check.status in {"ready", "insufficient"}
        assert "quotes" in check.result_data["dimensions"][0]
        assert db.scalar(select(EvidenceAttributionSpan.id)) is None


def _span_raw(
    *,
    turn_index: int,
    quote: str,
    owner: str,
    relation: str,
    start: int = 0,
) -> dict:
    return {
        "turn_index": turn_index,
        "quote": quote,
        "start": start,
        "end": start + len(quote),
        "text_hash": None,
        "owner": owner,
        "relation": relation,
        "elicitation_level": "open_probe",
        "source_label": None,
        "confidence": 0.9,
        "reason": "归属测试。",
    }


def test_server_eligibility_matrix_is_conservative() -> None:
    rows = [
        (1, "论文认为这个方案有效。", "external_paraphrased", "quotes_only"),
        (2, "我同意这个结论。", "participant_owned", "endorses"),
        (3, "我质疑它忽略了样本偏差。", "participant_owned", "critiques"),
        (4, "这句话的来源已经分不清了。", "uncertain", "quotes_only"),
        (5, "请AI帮我生成一个结论。", "participant_owned", "asks_or_requests"),
        (6, "我先核实来源，因为样本范围会影响结论。", "participant_owned", "own_reasoning"),
    ]
    output = EvidenceAttributionOutput.model_validate(
        {
            "spans": [
                _span_raw(
                    turn_index=index,
                    quote=quote,
                    owner=owner,
                    relation=relation,
                )
                for index, quote, owner, relation in rows
            ]
        }
    )
    validated = validate_attribution_output(
        output,
        [
            {"turn_index": index, "role": "user", "content": quote}
            for index, quote, _owner, _relation in rows
        ],
    )
    assert [item.eligibility for item in validated] == [
        "context_only",
        "context_only",
        "eligible",
        "manual_review",
        "context_only",
        "eligible",
    ]


def test_attribution_overlap_and_wrong_offsets_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        EvidenceAttributionOutput.model_validate(
            {
                "spans": [
                    _span_raw(
                        turn_index=1,
                        quote="前四个字",
                        owner="participant_owned",
                        relation="own_reasoning",
                    ),
                    _span_raw(
                        turn_index=1,
                        quote="个字后续",
                        owner="participant_owned",
                        relation="own_reasoning",
                        start=3,
                    ),
                ]
            }
        )
    output = EvidenceAttributionOutput.model_validate(
        {
            "spans": [
                _span_raw(
                    turn_index=1,
                    quote="第二段",
                    owner="participant_owned",
                    relation="own_reasoning",
                    start=0,
                )
            ]
        }
    )
    with pytest.raises(FinalizationError, match="do_not_match_quote"):
        validate_attribution_output(
            output,
            [{"turn_index": 1, "role": "user", "content": "第一段第二段"}],
        )


def _attributed_output(span_id: int) -> AttributedFinalScorerOutput:
    keys = (
        "problem_definition",
        "evidence_evaluation",
        "reasoning_argumentation",
        "multiple_perspectives",
        "integrative_decision",
        "dynamic_adjustment",
    )
    return AttributedFinalScorerOutput.model_validate(
        {
            "dimensions": [
                {
                    "dimension_key": key,
                    "score": 3 if index == 0 else None,
                    "evidence_refs": (
                        [{"attribution_span_id": span_id}] if index == 0 else []
                    ),
                    "reason": "只使用已验证 span。",
                    "confidence": 0.6 if index == 0 else 0.0,
                    "sufficient": index == 0,
                }
                for index, key in enumerate(keys)
            ],
            "strengths": [],
            "priorities": [],
        }
    )


def _persisted_span(**updates) -> EvidenceAttributionSpan:
    values = {
        "id": 7,
        "session_id": 1,
        "user_turn_id": 2,
        "readiness_check_id": 3,
        "turn_index": 1,
        "quote": "我先核实了数据来源。",
        "start": 0,
        "end": 12,
        "text_hash": attribution_text_hash("我先核实了数据来源。"),
        "owner": "participant_owned",
        "relation": "own_reasoning",
        "elicitation_level": "open_probe",
        "source_label": None,
        "confidence": 0.9,
        "reason": "自有推理。",
        "eligibility": "eligible",
        "validation_status": "validated",
        "validation_reason": "participant_reasoning_eligible",
        "transcript_fingerprint": "a" * 64,
        "asset_fingerprint": "b" * 64,
        "prompt_template_id": "natural_evidence_attribution_v6.2.1",
        "prompt_version": "v6.2.1",
        "schema_version": EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
    }
    values.update(updates)
    return EvidenceAttributionSpan(**values)


@pytest.mark.parametrize(
    ("span_updates", "span_id", "error"),
    [
        ({}, 99, "unknown_span"),
        ({"readiness_check_id": 8}, 7, "wrong_check"),
        ({"eligibility": "context_only"}, 7, "ineligible_span"),
        ({"transcript_fingerprint": "c" * 64}, 7, "stale_transcript"),
        ({"asset_fingerprint": "d" * 64}, 7, "stale_assets"),
        ({"text_hash": "0" * 64}, 7, "hash_mismatch"),
    ],
)
def test_attributed_scorer_refs_are_revalidated(
    span_updates: dict, span_id: int, error: str
) -> None:
    with pytest.raises(FinalizationError, match=error):
        InterviewOrchestrator._validate_attributed_output(
            _attributed_output(span_id),
            [_persisted_span(**span_updates)],
            expected_check_id=3,
            expected_transcript_fingerprint="a" * 64,
            expected_asset_fingerprint="b" * 64,
        )


def test_v621_navigation_requires_exact_user_anchor_and_returns_from_source_branch() -> None:
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "当时最难判断的是什么？"},
        {"turn_index": 1, "role": "user", "content": "AI说直接上线，但我觉得要先核实数据。"},
    ]
    with pytest.raises(InterviewContractError, match="navigation_required"):
        _validated_navigation(
            NaturalInterviewerOutput(
                interviewer_message="再说说你的判断。",
                session_action="continue",
                finish_reason=None,
            ),
            prompt_version="v6.2.1",
            transcript=transcript,
        )

    gateway = ModelGatewayService()
    first_payload = _interview_payload(transcript)
    assert first_payload["source_clarification_required"] is True
    first = gateway.generate_interviewer(
        first_payload, prompt_version="v6.2.1"
    ).output
    assert first.navigation.mainline_relation == "source_clarification"
    assert "哪些是外部材料" in first.interviewer_message
    assert first.navigation.decision_anchor.start == transcript[1]["content"].index(
        "但我觉得"
    )
    assert first.navigation.decision_anchor.model_dump(
        mode="json", exclude={"text_hash"}
    ) in first_payload["anchor_candidates"]
    returned_transcript = [
        *transcript,
        {
            "turn_index": 2,
            "role": "assistant",
            "content": first.interviewer_message,
        },
        {
            "turn_index": 3,
            "role": "user",
            "content": "AI的话是外部材料，我认为核实数据后再决定才是自己的判断。",
        },
    ]
    returned = gateway.generate_interviewer(
        _interview_payload(returned_transcript),
        prompt_version="v6.2.1",
    ).output
    assert source_clarification_required(returned_transcript) is False
    assert returned.navigation.mainline_relation == "return"

    bad = returned.model_copy(
        update={
            "navigation": returned.navigation.model_copy(
                update={
                    "decision_anchor": returned.navigation.decision_anchor.model_copy(
                        update={"start": 1, "end": 2, "quote": "不"}
                    )
                }
            )
        }
    )
    with pytest.raises(InterviewContractError, match="do_not_match_quote"):
        _validated_navigation(
            bad,
            prompt_version="v6.2.1",
            transcript=returned_transcript,
        )

    arbitrary_quote = "核实数据"
    arbitrary_start = returned_transcript[-1]["content"].index(arbitrary_quote)
    non_candidate = returned.model_copy(
        update={
            "navigation": returned.navigation.model_copy(
                update={
                    "decision_anchor": returned.navigation.decision_anchor.model_copy(
                        update={
                            "turn_index": 3,
                            "quote": arbitrary_quote,
                            "start": arbitrary_start,
                            "end": arbitrary_start + len(arbitrary_quote),
                            "text_hash": None,
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(InterviewContractError, match="not_in_server_candidates"):
        _validated_navigation(
            non_candidate,
            prompt_version="v6.2.1",
            transcript=returned_transcript,
        )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("AI建议立即上线，但我认为要先核实样本。", True),
        ("AI和我的说法混在一起，现在分不清来源。", True),
        ("AI建议先上线。", False),
        ("我认为要先核实样本。", False),
        ("我用AI核实了数据。", False),
        ("AI的建议是外部材料，我自己的判断是先核实样本。", False),
        ("外部材料来自论文，我的理由是它的样本不一致。", False),
    ],
)
def test_source_clarification_flag_boundary(content: str, expected: bool) -> None:
    transcript = [{"turn_index": 1, "role": "user", "content": content}]
    assert source_clarification_required(transcript) is expected


def test_source_clarification_flag_uses_latest_user_turn_and_case_15() -> None:
    transcript = [
        {"turn_index": 1, "role": "user", "content": CASE_15},
        {"turn_index": 2, "role": "assistant", "content": "哪些是外部材料？"},
    ]
    assert source_clarification_required(transcript) is True
    transcript.append(
        {
            "turn_index": 3,
            "role": "user",
            "content": "AI的话是外部材料，先核实数据才是自己的判断。",
        }
    )
    assert source_clarification_required(transcript) is False


def test_v621_gateway_rejects_missing_or_modified_anchor_candidates() -> None:
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "你当时怎么判断？"},
        {
            "turn_index": 1,
            "role": "user",
            "content": "AI建议立刻上线，但我认为要先核实样本。",
        },
    ]
    gateway = ModelGatewayService()
    with pytest.raises(ModelGatewayError, match="invalid_v621_anchor_candidates"):
        gateway.generate_interviewer(
            {"participant": {}, "transcript": transcript},
            prompt_version="v6.2.1",
        )
    payload = _interview_payload(transcript)
    payload["anchor_candidates"][0] = {
        **payload["anchor_candidates"][0],
        "start": 1,
        "end": payload["anchor_candidates"][0]["end"] + 1,
    }
    with pytest.raises(ModelGatewayError, match="invalid_v621_anchor_candidates"):
        gateway.generate_interviewer(payload, prompt_version="v6.2.1")

    flag_payload = _interview_payload(transcript)
    flag_payload["source_clarification_required"] = False
    with pytest.raises(
        ModelGatewayError,
        match="invalid_v621_source_clarification_flag",
    ):
        gateway.generate_interviewer(flag_payload, prompt_version="v6.2.1")


def test_v621_gateway_rejects_forged_false_positive_and_legacy_flags() -> None:
    transcript = [
        {"turn_index": 0, "role": "assistant", "content": "你当时怎么判断？"},
        {"turn_index": 1, "role": "user", "content": "我认为要先核实样本。"},
    ]
    gateway = ModelGatewayService()
    forged = _interview_payload(transcript)
    forged["source_clarification_required"] = True
    with pytest.raises(
        ModelGatewayError,
        match="invalid_v621_source_clarification_flag",
    ):
        gateway.generate_interviewer(forged, prompt_version="v6.2.1")

    with pytest.raises(
        ModelGatewayError,
        match="legacy_interview_payload_has_v621_navigation_inputs",
    ):
        gateway.generate_interviewer(forged, prompt_version="v6.2.0")

    with pytest.raises(
        ModelGatewayError,
        match="opening_must_not_require_source_clarification",
    ):
        gateway.generate_interviewer(
            {
                "participant": {},
                "transcript": [],
                "anchor_candidates": [],
                "source_clarification_required": True,
            },
            prompt_version="v6.2.1",
        )


def _source_clarification_output(
    payload: dict,
    *,
    message: str = "请区分哪些来自外部材料、哪些是你自己的判断，并说说采纳理由？",
    focus_kind: str = "source_ownership",
    mainline_relation: str = "source_clarification",
    anchor: dict | None = None,
) -> NaturalInterviewerOutput:
    selected_anchor = dict(anchor or payload["anchor_candidates"][-1])
    selected_anchor["text_hash"] = selected_anchor.get("text_hash")
    return NaturalInterviewerOutput.model_validate(
        {
            "interviewer_message": message,
            "session_action": "continue",
            "finish_reason": None,
            "navigation": {
                "decision_anchor": selected_anchor,
                "focus_kind": focus_kind,
                "mainline_relation": mainline_relation,
            },
        }
    )


@pytest.mark.parametrize(
    ("output_factory", "error"),
    [
        (
            lambda _payload: NaturalInterviewerOutput(
                interviewer_message="请区分外部材料和你自己的判断？",
                session_action="continue",
                finish_reason=None,
            ),
            "v621_navigation_required",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                message="哪些来自外部材料？哪些是你自己的判断？",
            ),
            "must_ask_one_primary_question",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                focus_kind="decision_problem",
                mainline_relation="core",
            ),
            "source_clarification_navigation_required",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                message="你更依赖AI还是自己的判断？",
            ),
            "source_clarification_must_not_be_binary",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                message="这些是外部材料或者你自己的判断？",
            ),
            "source_clarification_must_not_be_binary",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                message="请区分你自己的判断并说说采纳理由？",
            ),
            "external_signal_missing",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                message="请说明外部材料的来源？",
            ),
            "participant_signal_missing",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                anchor={
                    **payload["anchor_candidates"][-1],
                    "start": payload["anchor_candidates"][-1]["start"] + 1,
                    "end": payload["anchor_candidates"][-1]["end"] + 1,
                },
            ),
            "anchor_not_in_server_candidates",
        ),
        (
            lambda payload: _source_clarification_output(
                payload,
                anchor={
                    **payload["anchor_candidates"][-1],
                    "text_hash": "0" * 64,
                },
            ),
            "anchor_text_hash_must_be_null",
        ),
    ],
)
def test_v621_source_clarification_output_contract(
    output_factory, error: str
) -> None:
    payload = _interview_payload(
        [
            {"turn_index": 0, "role": "assistant", "content": "你当时怎么判断？"},
            {
                "turn_index": 1,
                "role": "user",
                "content": "AI建议立即上线，但我认为要先核实样本。",
            },
        ]
    )
    with pytest.raises(ValueError, match=error):
        _validate_v621_interviewer_contract(output_factory(payload), payload)


def test_v621_source_clarification_accepts_single_open_invitation_without_question_mark() -> None:
    payload = _interview_payload(
        [
            {"turn_index": 0, "role": "assistant", "content": "你当时怎么判断？"},
            {
                "turn_index": 1,
                "role": "user",
                "content": "AI建议立即上线，但我认为要先核实样本。",
            },
        ]
    )
    output = _source_clarification_output(
        payload,
        message="请区分外部材料和你自己的判断，并说明采纳理由。",
    )
    _validate_v621_interviewer_contract(output, payload)


def test_v621_source_contract_failure_is_repaired_inside_typed_call(
    monkeypatch,
) -> None:
    payload = _interview_payload(
        [
            {"turn_index": 0, "role": "assistant", "content": "你当时怎么判断？"},
            {
                "turn_index": 1,
                "role": "user",
                "content": "AI建议立即上线，但我认为要先核实样本。",
            },
        ]
    )
    invalid = _source_clarification_output(
        payload,
        focus_kind="decision_problem",
        mainline_relation="core",
    ).model_dump(mode="json")
    valid = _source_clarification_output(payload).model_dump(mode="json")
    captured_messages: list[list[dict[str, str]]] = []

    def fake_post_json(messages, **_kwargs):
        captured_messages.append(
            json.loads(json.dumps(messages, ensure_ascii=False))
        )
        return invalid if len(captured_messages) == 1 else valid

    gateway = ModelGatewayService()
    gateway.mode = "real"
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(gateway, "_post_json", fake_post_json)
    call = gateway.generate_interviewer(payload, prompt_version="v6.2.1")

    assert call.repair_used is True
    assert call.attempt_count == 2
    assert len(captured_messages) == 2
    assert "v621_source_clarification_navigation_required" in (
        captured_messages[1][-1]["content"]
    )
    assert call.output.navigation.focus_kind == "source_ownership"
    assert call.output.navigation.mainline_relation == "source_clarification"


def test_shadow_records_comparison_but_keeps_raw_result(client, monkeypatch) -> None:
    _configure_v621(monkeypatch, "shadow")
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    with TestSession() as db:
        check = db.scalar(select(EvidenceReadinessCheck))
        assert check is not None
        assert "quotes" in check.result_data["dimensions"][0]
        assert db.scalar(select(EvidenceAttributionSpan.id)) is not None
        traces = list(db.scalars(select(AgentTrace)))
        attributed_trace = next(
            trace
            for trace in traces
            if trace.action == "attributed_evidence_snapshot_shadow"
        )
        assert (
            attributed_trace.output_contract["schema_version"]
            == ATTRIBUTED_EVIDENCE_SCHEMA_VERSION
        )
        attribution_trace = next(
            trace
            for trace in traces
            if trace.action == "evidence_attribution_snapshot"
        )
        assert (
            attribution_trace.output_contract["schema_version"]
            == EVIDENCE_ATTRIBUTION_SCHEMA_VERSION
        )
        assert (
            attribution_trace.output_contract["candidate_rule_version"]
            == EVIDENCE_CANDIDATE_RULE_VERSION
        )


def test_shadow_attribution_failure_falls_back_with_explicit_trace(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "shadow")
    gateway = router_module.sessions.orchestrator.gateway
    monkeypatch.setattr(
        gateway,
        "generate_evidence_attribution",
        lambda _payload: (_ for _ in ()).throw(ModelGatewayError("broken")),
    )
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    with TestSession() as db:
        check = db.scalar(select(EvidenceReadinessCheck))
        assert check is not None and check.status in {"ready", "insufficient"}
        assert "quotes" in check.result_data["dimensions"][0]
        failed = db.scalar(
            select(AgentTrace).where(
                AgentTrace.action == "evidence_attribution_failed"
            )
        )
        assert failed is not None and failed.fallback_used is True
        assert db.scalar(
            select(AssessmentSession.phase).where(
                AssessmentSession.uuid == session_uuid
            )
        ) == "interviewing"


def test_shadow_double_failure_preserves_both_traces_without_scoring(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "shadow")
    gateway = router_module.sessions.orchestrator.gateway
    monkeypatch.setattr(
        gateway,
        "generate_evidence_attribution",
        lambda _payload: (_ for _ in ()).throw(
            ModelGatewayError("attribution_failed")
        ),
    )
    monkeypatch.setattr(
        gateway,
        "generate_incremental_evidence",
        lambda _payload: (_ for _ in ()).throw(
            ModelGatewayError("legacy_failed")
        ),
    )

    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")

    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(
                AssessmentSession.uuid == session_uuid
            )
        )
        check = db.scalar(select(EvidenceReadinessCheck))
        failed_traces = list(
            db.scalars(
                select(AgentTrace)
                .where(AgentTrace.renderer_status == "failed")
                .order_by(AgentTrace.id)
            )
        )
        assert session is not None and session.phase == "interviewing"
        assert check is not None and check.status == "failed"
        assert check.ready is None
        assert check.result_data is None
        assert [trace.action for trace in failed_traces] == [
            "evidence_attribution_failed",
            "legacy_incremental_scoring_failed",
        ]
        assert failed_traces[0].fallback_used is True
        assert failed_traces[1].fallback_used is False
        assert db.scalar(select(EvidenceAttributionSpan.id)) is None
        assert db.scalar(select(EvidenceItem.id)) is None


def test_strong_scaffold_evidence_scores_but_recommends_manual_review(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    gateway = router_module.sessions.orchestrator.gateway
    original = gateway.generate_evidence_attribution

    def force_strong(payload):
        call = original(payload)
        raw = call.output.model_dump(mode="json")
        for span in raw["spans"]:
            span["elicitation_level"] = "strong_scaffold"
        return replace(
            call, output=EvidenceAttributionSelectionOutput.model_validate(raw)
        )

    monkeypatch.setattr(gateway, "generate_evidence_attribution", force_strong)
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    with TestSession() as db:
        check = db.scalar(select(EvidenceReadinessCheck))
        assert check is not None
        check_id = check.id
        fingerprint = check.transcript_fingerprint
    finalized = client.post(
        f"/api/v1/sessions/{session_uuid}/finalize",
        json={
            "evidence_check_id": check_id,
            "expected_transcript_fingerprint": fingerprint,
            "allow_incomplete": True,
        },
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["session"]["manual_review_recommended"] is True
    assert finalized.json()["report"]["manual_review_recommended"] is True
    with TestSession() as db:
        evidence = list(db.scalars(select(EvidenceItem)))
        assert evidence and any(
            item.validation_reason == "eligible_strong_scaffold_manual_review"
            for item in evidence
        )


def test_old_session_asset_is_stable_and_new_prompt_assets_are_frozen(
    monkeypatch,
) -> None:
    legacy = _report_readiness_asset_fingerprint("disabled")
    assert legacy == "1fd58de4bf1ff061acb34630fa97966253a09952acb1ad028f927d1972826063"
    monkeypatch.setattr(settings, "evidence_attribution_mode", "shadow")
    assert _report_readiness_asset_fingerprint("disabled") == legacy
    monkeypatch.setattr(settings, "evidence_attribution_mode", "enforce")
    assert _report_readiness_asset_fingerprint("disabled") == legacy
    shadow_asset = _report_readiness_asset_fingerprint("shadow")
    monkeypatch.setattr(
        session_service_module,
        "ATTRIBUTED_EVIDENCE_SCHEMA_VERSION",
        "attributed-evidence-span-ref-v2-test",
    )
    assert _report_readiness_asset_fingerprint("shadow") != shadow_asset
    assert _report_readiness_asset_fingerprint("disabled") == legacy
    attributed_schema_asset = _report_readiness_asset_fingerprint("shadow")
    monkeypatch.setattr(
        session_service_module,
        "EVIDENCE_ATTRIBUTION_SCHEMA_VERSION",
        "evidence-attribution-span-v3-test",
    )
    assert (
        _report_readiness_asset_fingerprint("shadow")
        != attributed_schema_asset
    )
    assert _report_readiness_asset_fingerprint("disabled") == legacy
    candidate_rule_asset = _report_readiness_asset_fingerprint("shadow")
    monkeypatch.setattr(
        session_service_module,
        "EVIDENCE_CANDIDATE_RULE_VERSION",
        "evidence-span-boundaries-v2-test",
    )
    assert _report_readiness_asset_fingerprint("shadow") != candidate_rule_asset
    assert _report_readiness_asset_fingerprint("disabled") == legacy
    assert hashlib.sha256(
        NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_0.encode("utf-8")
    ).hexdigest() == "40de5708ff67e05772b408a77f38c5edce67ceb352d660f777044e793954adca"
    assert hashlib.sha256(
        NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_1.encode("utf-8")
    ).hexdigest() == "a2782644f1701c6eefb057e391a062d94791d08805b8a6dd784a9b359e4f7c83"
    assert "interviewer_message 最多出现一个问号" in (
        NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_1
    )
    assert "不得使用“更依赖 A 还是 B”" in (
        NATURAL_INTERVIEWER_SYSTEM_PROMPT_V6_2_1
    )
    assert hashlib.sha256(
        EVIDENCE_ATTRIBUTION_SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest() == "0a6d49a63cdcceed7792ebca1ae9ce2097adfd23f775113facb71b5ae201e113"
    assert hashlib.sha256(
        ATTRIBUTED_EVIDENCE_SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest() == "09863931f0721c464493a43fde45b724044d5e32bc6d97dd0dda6db909c554f4"


@pytest.mark.parametrize(
    "content",
    [
        "AI建议立刻上线，但我不同意，因为还没有核实样本偏差。",
        "论文说这样最好，但我不会直接采用，因为它的样本范围不同。",
    ],
)
def test_mock_splits_external_claim_from_participant_rejection(content: str) -> None:
    payload = _attribution_payload(
        [
            {
                "turn_index": 1,
                "content": content,
                "preceding_question": "你怎么看这个建议？",
            }
        ]
    )
    output = _materialized_mock_output(payload)
    validated = validate_attribution_output(
        output, [{"turn_index": 1, "role": "user", "content": content}]
    )
    assert len(validated) == 2
    assert validated[0].eligibility == "context_only"
    assert validated[1].eligibility == "eligible"
    assert validated[1].output.relation == "rejects"


def test_session_binds_attribution_mode_at_opening(client, monkeypatch) -> None:
    _configure_v621(monkeypatch, "shadow")
    shadow_uuid = _create_session(client)
    monkeypatch.setattr(settings, "evidence_attribution_mode", "enforce")
    _submit(
        client,
        shadow_uuid,
        "我会先核实数据来源，再根据反例调整决定。",
    )
    with TestSession() as db:
        shadow_session = db.scalar(
            select(AssessmentSession).where(
                AssessmentSession.uuid == shadow_uuid
            )
        )
        shadow_check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == shadow_session.id
            )
        )
        assert "quotes" in shadow_check.result_data["dimensions"][0]
        opening = db.scalar(
            select(AgentTrace).where(
                AgentTrace.session_id == shadow_session.id,
                AgentTrace.action == "natural_opening",
            )
        )
        assert opening.output_contract["evidence_attribution_mode"] == "shadow"

    enforce_uuid = _create_session(client)
    monkeypatch.setattr(settings, "evidence_attribution_mode", "disabled")
    _submit(
        client,
        enforce_uuid,
        "我会先核实数据来源，再根据反例调整决定。",
        suffix="00000002",
    )
    with TestSession() as db:
        enforce_session = db.scalar(
            select(AssessmentSession).where(
                AssessmentSession.uuid == enforce_uuid
            )
        )
        enforce_check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == enforce_session.id
            )
        )
        assert "evidence_refs" in enforce_check.result_data["dimensions"][0]


def test_v620_cached_snapshot_finalizes_after_global_mode_switch(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "natural_interviewer_prompt_version", "v6.2.0")
    monkeypatch.setattr(settings, "evidence_attribution_mode", "disabled")
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    with TestSession() as db:
        check = db.scalar(select(EvidenceReadinessCheck))
        assert check.asset_fingerprint == _report_readiness_asset_fingerprint(
            "disabled"
        )
        check_id = check.id
        fingerprint = check.transcript_fingerprint

    monkeypatch.setattr(settings, "evidence_attribution_mode", "enforce")
    finalized = client.post(
        f"/api/v1/sessions/{session_uuid}/finalize",
        json={
            "evidence_check_id": check_id,
            "expected_transcript_fingerprint": fingerprint,
            "allow_incomplete": True,
        },
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["session"]["phase"] == "completed"


def test_failed_enforce_snapshot_retries_and_stale_worker_cannot_overwrite(
    client, monkeypatch
) -> None:
    _configure_v621(monkeypatch, "enforce")
    gateway = router_module.sessions.orchestrator.gateway
    original = gateway.generate_evidence_attribution
    monkeypatch.setattr(
        gateway,
        "generate_evidence_attribution",
        lambda _payload: (_ for _ in ()).throw(ModelGatewayError("first_failed")),
    )
    session_uuid = _create_session(client)
    _submit(client, session_uuid, "我会先核实数据来源，再根据反例调整决定。")
    monkeypatch.setattr(gateway, "generate_evidence_attribution", original)
    retried = client.post(
        f"/api/v1/sessions/{session_uuid}/report-readiness?retry_failed=true"
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "insufficient"

    with TestSession() as db:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == session.id
            )
        )
        check.status = "processing"
        check.created_at = utcnow()
        check.ready = None
        db.commit()
        check_id = check.id
        current_token = _lease_token(check.created_at)
        transcript = [
            {
                "turn_index": turn.turn_index,
                "role": turn.role,
                "content": turn.content,
            }
            for turn in session.turns
        ]

    called = False

    def should_not_run(_payload):
        nonlocal called
        called = True
        raise AssertionError("stale worker reached model")

    monkeypatch.setattr(gateway, "generate_evidence_attribution", should_not_run)
    router_module.sessions._execute_evidence_snapshot(
        TestSession,
        check_id,
        None,
        [],
        transcript,
        "stale-" + current_token,
    )
    assert called is False
    with TestSession() as db:
        check = db.get(EvidenceReadinessCheck, check_id)
        assert check.status == "processing"
        assert _lease_token(check.created_at) == current_token
