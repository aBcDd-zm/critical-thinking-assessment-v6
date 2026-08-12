"""Session lifecycle for the V6 natural interview."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import time
from collections.abc import Callable
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AgentTrace,
    AssessmentSession,
    DialogueTurn,
    EvidenceAttributionSpan,
    EvidenceReadinessCheck,
    HumanReview,
    TechnicalAnomaly,
    TurnSubmission,
    utcnow,
)
from app.schemas import (
    MIN_ANSWER_VISIBLE_CHARACTERS,
    AttributedFinalScorerOutput,
    CreateSessionRequest,
    FinalScorerOutput,
    FinalizeSessionRequest,
    SubmitTurnRequest,
    is_explicit_uncertainty_answer,
    visible_character_count,
)
from app.services.model_gateway import (
    ATTRIBUTED_EVIDENCE_PROMPT_ID,
    ATTRIBUTED_EVIDENCE_PROMPT_VERSION,
    ATTRIBUTED_EVIDENCE_SCHEMA_VERSION,
    ATTRIBUTED_EVIDENCE_SYSTEM_PROMPT,
    EVIDENCE_ATTRIBUTION_PROMPT_ID,
    EVIDENCE_ATTRIBUTION_PROMPT_VERSION,
    EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
    EVIDENCE_ATTRIBUTION_SYSTEM_PROMPT,
    EVIDENCE_CANDIDATE_RULE_VERSION,
    INCREMENTAL_EVIDENCE_SYSTEM_PROMPT,
    INCREMENTAL_EVIDENCE_PROMPT_ID,
    INCREMENTAL_EVIDENCE_PROMPT_VERSION,
    ModelGatewayError,
    StructuredCallResult,
    resolve_natural_interviewer_prompt,
)
from app.services.orchestrator import (
    ATTRIBUTION_AWARE_INTERVIEWER_PROMPT_VERSIONS,
    EVIDENCE_GATED_INTERVIEWER_PROMPT_VERSIONS,
    FinalizationError,
    InterviewContractError,
    InterviewOrchestrator,
    SUGGEST_FINISH_ACTION,
    TECHNICAL_TURN_CAP_ACKNOWLEDGEMENT_PROMPT_ID,
    transcript_fingerprint,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
TECHNICAL_USER_TURN_CAP = 40
REPORT_READINESS_RULE_VERSION = "incremental-evidence-v3-min8-strict"
EVIDENCE_ELIGIBILITY_RULE_VERSION = "participant-owned-reasoning-v1"
# This is a minimum, not the production lease itself. The effective lease also
# covers every sequential evidence-model budget for the mode bound at opening:
# enforce has two calls, while historical shadow sessions still have three.
REPORT_READINESS_LEASE_SECONDS = 60
REPORT_READINESS_LEASE_GRACE_SECONDS = 30
LEGACY_INTERVIEWER_PROMPT_VERSION = "v6.0.5"
_evidence_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="v6-evidence",
)
_queued_evidence_tasks: set[tuple[int, str]] = set()
_queued_evidence_tasks_lock = threading.Lock()


def _lease_token(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _register_queued_evidence_task(check_id: int, lease_token: str) -> None:
    with _queued_evidence_tasks_lock:
        _queued_evidence_tasks.add((check_id, lease_token))


def _discard_queued_evidence_task(check_id: int, lease_token: str | None) -> None:
    if lease_token is None:
        return
    with _queued_evidence_tasks_lock:
        _queued_evidence_tasks.discard((check_id, lease_token))


def _queued_evidence_task_is_live(check_id: int, lease_token: str) -> bool:
    with _queued_evidence_tasks_lock:
        return (check_id, lease_token) in _queued_evidence_tasks


def _report_readiness_lease_seconds(effective_mode: str) -> float:
    """Keep a worker lease beyond the bound mode's worst-case model budget."""

    stage_count = {
        "disabled": 1,
        "enforce": 2,
        "shadow": 3,
    }.get(effective_mode, 1)
    configured_budget = settings.deepseek_evidence_total_timeout_seconds * stage_count
    return max(
        float(REPORT_READINESS_LEASE_SECONDS),
        configured_budget + REPORT_READINESS_LEASE_GRACE_SECONDS,
    )


def _start_evidence_snapshot_lease(
    db: Session,
    check_id: int,
    queued_lease_token: str | None,
) -> str | None:
    """Atomically exchange an enqueue token for a fresh running lease.

    ``created_at`` is the existing lease clock. A queued task is protected by
    the in-process registration above, while a worker refreshes the clock only
    when an executor thread actually starts it. The conditional UPDATE keeps a
    reclaimed/stale worker from acquiring the replacement task's lease.
    """

    try:
        check = db.get(EvidenceReadinessCheck, check_id)
        if check is None or check.status != "processing":
            return None
        current_token = _lease_token(check.created_at)
        if queued_lease_token is not None and current_token != queued_lease_token:
            return None
        if check.model_provider != "queued":
            # Direct callers and already-started workers retain the normal
            # token check without refreshing a running lease a second time.
            return current_token

        started_at = utcnow()
        if _lease_token(started_at) == current_token:
            started_at += timedelta(microseconds=1)
        claimed = db.execute(
            update(EvidenceReadinessCheck)
            .where(
                EvidenceReadinessCheck.id == check_id,
                EvidenceReadinessCheck.status == "processing",
                EvidenceReadinessCheck.model_provider == "queued",
                EvidenceReadinessCheck.created_at == check.created_at,
            )
            .values(
                model_provider="pending",
                created_at=started_at,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        db.expire_all()
        return _lease_token(started_at)
    finally:
        _discard_queued_evidence_task(check_id, queued_lease_token)


def _report_readiness_asset_fingerprint(
    effective_mode: str | None = None,
) -> str:
    """Hash only assets that can affect this session's bound contract."""

    mode = effective_mode or settings.evidence_attribution_mode
    assets: dict[str, Any] = {
        "gateway_mode": settings.model_gateway_mode,
        "model": settings.deepseek_model,
        "prompt_template_id": INCREMENTAL_EVIDENCE_PROMPT_ID,
        "prompt_version": INCREMENTAL_EVIDENCE_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(
            INCREMENTAL_EVIDENCE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "readiness_rule_version": REPORT_READINESS_RULE_VERSION,
        "minimum_user_turns": settings.natural_interview_min_user_turns,
    }
    if mode in {"shadow", "enforce"}:
        assets.update(
            {
                "effective_attribution_mode": mode,
                "attribution_prompt_template_id": EVIDENCE_ATTRIBUTION_PROMPT_ID,
                "attribution_prompt_version": EVIDENCE_ATTRIBUTION_PROMPT_VERSION,
                "attribution_prompt_sha256": hashlib.sha256(
                    EVIDENCE_ATTRIBUTION_SYSTEM_PROMPT.encode("utf-8")
                ).hexdigest(),
                "attribution_schema_version": EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
                "evidence_candidate_rule_version": EVIDENCE_CANDIDATE_RULE_VERSION,
                "eligibility_rule_version": EVIDENCE_ELIGIBILITY_RULE_VERSION,
                "attributed_scorer_prompt_template_id": ATTRIBUTED_EVIDENCE_PROMPT_ID,
                "attributed_scorer_prompt_version": ATTRIBUTED_EVIDENCE_PROMPT_VERSION,
                "attributed_scorer_prompt_sha256": hashlib.sha256(
                    ATTRIBUTED_EVIDENCE_SYSTEM_PROMPT.encode("utf-8")
                ).hexdigest(),
                "attributed_scorer_schema_version": (
                    ATTRIBUTED_EVIDENCE_SCHEMA_VERSION
                ),
            }
        )
    canonical = json.dumps(
        assets,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_report_readiness(
    check: EvidenceReadinessCheck, *, cached: bool, saved_user_answer_count: int
) -> dict[str, Any]:
    """Return only participant-safe aggregate readiness information."""

    status = (
        check.status
        if check.status in {"ready", "insufficient", "failed"}
        else "checking"
    )
    return {
        "status": status,
        "ready": check.ready if status in {"ready", "insufficient"} else None,
        "cached": cached,
        "check_id": check.id,
        "transcript_fingerprint": check.transcript_fingerprint,
        "minimum_turns_required": settings.natural_interview_min_user_turns,
        "minimum_turns_met": (
            saved_user_answer_count >= settings.natural_interview_min_user_turns
        ),
    }


def _session_transcript_rows(session: AssessmentSession) -> list[dict[str, Any]]:
    return [
        {
            "turn_index": turn.turn_index,
            "role": turn.role,
            "content": turn.content,
        }
        for turn in sorted(session.turns, key=lambda item: item.turn_index)
        if turn.role in {"user", "assistant"}
    ]


def serialize_turn(turn: DialogueTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "turn_index": turn.turn_index,
        "role": turn.role,
        "content": turn.content,
        "client_turn_id": turn.client_turn_id,
        "input_mode": turn.input_mode,
        "answer_duration_ms": turn.answer_duration_ms,
        "phase": turn.phase,
        "session_action": turn.session_action,
        "finish_reason": turn.finish_reason,
        "quality_flags": list(turn.quality_flags or []),
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
    }


def serialize_report(session: AssessmentSession) -> dict[str, Any] | None:
    if not session.report:
        return None
    data = deepcopy(session.report.report_data)
    answer_ordinal_by_turn_index = {
        turn.turn_index: ordinal
        for ordinal, turn in enumerate(
            (
                turn
                for turn in sorted(session.turns, key=lambda item: item.turn_index)
                if turn.role == "user"
            ),
            start=1,
        )
    }
    dimensions = data.get("dimensions")
    if isinstance(dimensions, list):
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            evidences = dimension.get("evidences")
            if not isinstance(evidences, list):
                continue
            for evidence in evidences:
                if not isinstance(evidence, dict):
                    continue
                turn_index = evidence.get("turn_index")
                if isinstance(turn_index, int):
                    answer_ordinal = answer_ordinal_by_turn_index.get(turn_index)
                    if answer_ordinal is not None:
                        evidence["answer_ordinal"] = answer_ordinal
    data["generated_at"] = session.report.created_at.isoformat()
    return data


def active_closure_suggestion(session: AssessmentSession) -> dict[str, Any] | None:
    """Return the current model-authored close suggestion, if it is still current.

    A suggestion is active only while it is the latest persisted turn in an
    interviewing session.  Any subsequent participant answer supersedes it
    without adding mutable session-level state.
    """

    if session.phase != "interviewing":
        return None
    turns = sorted(session.turns, key=lambda item: item.turn_index)
    if not turns:
        return None
    latest = turns[-1]
    if (
        latest.role != "assistant"
        or latest.session_action != SUGGEST_FINISH_ACTION
        or latest.id is None
    ):
        return None
    return {
        "closure_turn_id": latest.id,
        "transcript_fingerprint": transcript_fingerprint(session),
        "finish_reason": latest.finish_reason,
    }


def session_snapshot(session: AssessmentSession) -> dict[str, Any]:
    turns = sorted(session.turns, key=lambda item: item.turn_index)
    return {
        "uuid": session.uuid,
        "phase": session.phase,
        "consent_version": session.consent_version,
        "consent_accepted_at": (
            session.consent_accepted_at.isoformat()
            if session.consent_accepted_at
            else None
        ),
        "participant": {
            "display_name": session.display_name or "",
            "identity_type": (session.profile_data or {}).get("identity_type", "other"),
            "occupation": session.occupation or "",
            "experience_level": session.experience_level or "",
            "collaboration_role": session.collaboration_role or "",
        },
        "user_answer_count": session.user_answer_count,
        "technical_turn_cap": TECHNICAL_USER_TURN_CAP,
        "technical_turn_cap_reached": (
            (session.user_answer_count or 0) >= TECHNICAL_USER_TURN_CAP
        ),
        "transcript_fingerprint": session.transcript_fingerprint,
        "transcript_frozen_at": (
            session.transcript_frozen_at.isoformat()
            if session.transcript_frozen_at
            else None
        ),
        "finalization_state": session.finalization_state,
        "closure_suggestion": active_closure_suggestion(session),
        "manual_review_recommended": session.manual_review_recommended,
        "experimental_notice": (
            "思衡 V6 是探索性、非标准化的自然访谈演示，"
            "不支持跨用户比较或正式效度结论。"
        ),
        "current_turn": serialize_turn(turns[-1]) if turns else None,
        "turns": [serialize_turn(turn) for turn in turns],
        "report_available": session.report is not None,
        "ended_early": session.ended_early,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
    }


class SessionService:
    def __init__(self, orchestrator: InterviewOrchestrator | None = None) -> None:
        self.orchestrator = orchestrator or InterviewOrchestrator()

    def create(
        self, db: Session, request: CreateSessionRequest
    ) -> tuple[AssessmentSession, DialogueTurn]:
        if not request.consent_given:
            raise ServiceError(422, "consent_required", "必须同意当前版本知情同意才能开始。")
        participant = request.participant
        session = AssessmentSession(
            phase="interviewing",
            consent_version=request.consent_version,
            consent_accepted_at=utcnow(),
            display_name=participant.display_name.strip() or None,
            occupation=participant.occupation.strip() or None,
            experience_level=participant.experience_level.strip() or None,
            collaboration_role=participant.collaboration_role.strip() or None,
            profile_data={"identity_type": participant.identity_type},
        )
        db.add(session)
        db.flush()
        participant_payload = {
            "display_name": session.display_name or "",
            "identity_type": participant.identity_type,
            "occupation": session.occupation or "",
            "experience_level": session.experience_level or "",
            "collaboration_role": session.collaboration_role or "",
        }
        try:
            opening = self.orchestrator.opening(
                participant_payload,
                prompt_version=settings.natural_interviewer_prompt_version,
            )
        except (ModelGatewayError, InterviewContractError) as exc:
            db.rollback()
            raise ServiceError(
                503, "opening_generation_failed", "开场生成暂时失败，请稍后重试开始。"
            ) from exc
        initial = DialogueTurn(
            session_id=session.id,
            turn_index=0,
            role="assistant",
            phase="interviewing",
            content=opening.content,
            session_action=opening.session_action,
            finish_reason=opening.finish_reason,
            quality_flags=opening.quality_flags,
        )
        db.add(initial)
        db.flush()
        db.add(
            AgentTrace(
                session_id=session.id,
                assistant_turn_id=initial.id,
                action="natural_opening",
                model_provider=opening.provider,
                model_name=opening.model,
                prompt_template_id=opening.prompt_template_id,
                prompt_version=opening.prompt_version,
                input_fingerprint=opening.input_fingerprint,
                output_contract={
                    "session_action": opening.session_action,
                    "finish_reason": opening.finish_reason,
                    "quality_flags": opening.quality_flags,
                    "attempt_count": opening.attempt_count,
                    "evidence_attribution_mode": (
                        settings.evidence_attribution_mode
                        if opening.prompt_version
                        in ATTRIBUTION_AWARE_INTERVIEWER_PROMPT_VERSIONS
                        else "disabled"
                    ),
                },
                renderer_status="repaired" if opening.repair_used else "accepted",
                repair_used=opening.repair_used,
                latency_ms=opening.latency_ms,
            )
        )
        db.add(HumanReview(session_id=session.id, status="pending"))
        db.commit()
        db.refresh(session)
        db.refresh(initial)
        return session, initial

    def get(self, db: Session, session_uuid: str) -> AssessmentSession:
        session = db.scalar(
            select(AssessmentSession).where(AssessmentSession.uuid == session_uuid)
        )
        if not session:
            raise ServiceError(404, "session_not_found", "会话不存在。")
        return session

    @staticmethod
    def _bound_interviewer_prompt_version(db: Session, session_id: int) -> str:
        """Keep every turn on the prompt version recorded by its opening.

        Sessions created before opening traces existed conservatively remain on
        the previous production baseline instead of inheriting a deployment
        switch halfway through the conversation.
        """

        recorded = db.scalar(
            select(AgentTrace.prompt_version)
            .where(
                AgentTrace.session_id == session_id,
                AgentTrace.action == "natural_opening",
            )
            .order_by(AgentTrace.id.asc())
            .limit(1)
        )
        candidate = str(recorded or LEGACY_INTERVIEWER_PROMPT_VERSION)
        try:
            resolve_natural_interviewer_prompt(candidate)
        except ValueError:
            return LEGACY_INTERVIEWER_PROMPT_VERSION
        return candidate

    @classmethod
    def _effective_attribution_mode(
        cls, db: Session, session_id: int
    ) -> str:
        """Never switch an existing legacy session onto attribution midstream."""

        opening_trace = db.scalar(
            select(AgentTrace)
            .where(
                AgentTrace.session_id == session_id,
                AgentTrace.action == "natural_opening",
            )
            .order_by(AgentTrace.id.asc())
            .limit(1)
        )
        if (
            opening_trace is None
            or opening_trace.prompt_version
            not in ATTRIBUTION_AWARE_INTERVIEWER_PROMPT_VERSIONS
        ):
            return "disabled"
        bound = (opening_trace.output_contract or {}).get(
            "evidence_attribution_mode"
        )
        if bound not in {"disabled", "shadow", "enforce"}:
            # Rows created before the binding field existed remain legacy. A
            # deployment switch must never reinterpret an open conversation.
            return "disabled"
        return str(bound)

    @staticmethod
    def _payload_hash(request: SubmitTurnRequest) -> str:
        return hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _next_turn_index(db: Session, session_id: int) -> int:
        current = db.scalar(
            select(func.max(DialogueTurn.turn_index)).where(
                DialogueTurn.session_id == session_id
            )
        )
        return int(current if current is not None else -1) + 1

    def _completed_events(
        self,
        session: AssessmentSession,
        user_turn: DialogueTurn,
        assistant_turn: DialogueTurn,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {"event": "user_turn_saved", "data": {"turn": serialize_turn(user_turn)}},
            {"event": "agent_started", "data": {"phase": "interviewing"}},
            {"event": "agent_delta", "delta": assistant_turn.content},
        ]
        if assistant_turn.session_action == "finish" and session.phase == "finalizing":
            events.append(
                {
                    "event": "session_finalizing",
                    "data": {
                        "session_uuid": session.uuid,
                        "finish_reason": assistant_turn.finish_reason,
                    },
                }
            )
        suggestion = active_closure_suggestion(session)
        if (
            assistant_turn.session_action == SUGGEST_FINISH_ACTION
            and suggestion is not None
            and suggestion["closure_turn_id"] == assistant_turn.id
        ):
            events.append(
                {
                    "event": "session_closure_suggested",
                    "data": {
                        "session_uuid": session.uuid,
                        **suggestion,
                    },
                }
            )
        events.append(
            {
                "event": "agent_completed",
                "data": {
                    "turn": serialize_turn(assistant_turn),
                    "session_action": assistant_turn.session_action,
                    "finish_reason": assistant_turn.finish_reason,
                    "speech_url": (
                        f"/api/v1/sessions/{session.uuid}/turns/"
                        f"{assistant_turn.turn_index}/speech"
                    ),
                    "session": session_snapshot(session),
                },
            }
        )
        return events

    def submit(
        self,
        db: Session,
        session_uuid: str,
        request: SubmitTurnRequest,
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        payload_hash = self._payload_hash(request)
        with _locks[session_uuid]:
            session = self.get(db, session_uuid)
            existing = db.scalar(
                select(TurnSubmission).where(
                    TurnSubmission.session_id == session.id,
                    TurnSubmission.client_turn_id == request.client_turn_id,
                )
            )
            if existing and existing.payload_hash != payload_hash:
                raise ServiceError(
                    409,
                    "idempotency_payload_mismatch",
                    "同一 client_turn_id 不能用于不同载荷。",
                )
            if existing and existing.state == "completed" and existing.response_events:
                if emit:
                    for event in existing.response_events:
                        emit(event)
                return existing.response_events
            if existing:
                # A previously persisted assistant turn is authoritative.  It
                # can be replayed even if the natural close already moved the
                # session into finalizing/completed, so a network interruption
                # never calls the interviewer twice for the same user turn.
                submission = existing
                submission.state = "processing"
                submission.recovery_count += 1
                submission.error_message = None
                if submission.assistant_turn_id:
                    user_turn = db.get(DialogueTurn, submission.user_turn_id)
                    assistant_turn = db.get(DialogueTurn, submission.assistant_turn_id)
                    if user_turn and assistant_turn:
                        events = self._completed_events(session, user_turn, assistant_turn)
                        submission.response_events = events
                        submission.state = "completed"
                        submission.completed_at = utcnow()
                        db.commit()
                        if emit:
                            for event in events:
                                emit(event)
                        return events
            if session.phase != "interviewing":
                raise ServiceError(
                    409, "session_not_accepting_turns", "当前会话不再接收作答。"
                )
            # The cap only blocks a new answer.  A saved failed submission must
            # remain recoverable with the same idempotency key, including when
            # that saved answer happened to be the fortieth one.
            if not existing and session.user_answer_count >= TECHNICAL_USER_TURN_CAP:
                raise ServiceError(
                    409,
                    "technical_turn_cap_reached",
                    "本次访谈已达到技术保护上限，请结束并生成报告。",
                )
            if (
                not existing
                and (session.user_answer_count or 0) > 0
                and visible_character_count(request.content)
                < MIN_ANSWER_VISIBLE_CHARACTERS
                and not is_explicit_uncertainty_answer(request.content)
            ):
                raise ServiceError(
                    422,
                    "answer_too_short",
                    (
                        f"从第二个回答起，每次回答至少需要 "
                        f"{MIN_ANSWER_VISIBLE_CHARACTERS} 个字；"
                        "如果确实还不知道，也可以直接这样告诉我。"
                    ),
                )

            if existing:
                submission = existing
            else:
                submission = TurnSubmission(
                    session_id=session.id,
                    client_turn_id=request.client_turn_id,
                    payload_hash=payload_hash,
                    state="processing",
                    processing_started_at=utcnow(),
                )
                db.add(submission)
            db.commit()
            db.refresh(submission)

            try:
                if submission.user_turn_id:
                    user_turn = db.get(DialogueTurn, submission.user_turn_id)
                    if not user_turn:
                        raise RuntimeError("persisted_user_turn_missing")
                else:
                    user_turn = DialogueTurn(
                        session_id=session.id,
                        turn_index=self._next_turn_index(db, session.id),
                        role="user",
                        phase="interviewing",
                        content=request.content.strip(),
                        client_turn_id=request.client_turn_id,
                        input_mode=request.input_mode,
                        answer_duration_ms=request.answer_duration_ms,
                        quality_flags=[],
                    )
                    db.add(user_turn)
                    db.flush()
                    submission.user_turn_id = user_turn.id
                    session.user_answer_count += 1
                    for detail in request.anomaly_details:
                        db.add(
                            TechnicalAnomaly(
                                session_id=session.id,
                                turn_id=user_turn.id,
                                category="client_input",
                                detail=detail,
                                recoverable=True,
                            )
                        )
                    db.commit()
                    db.refresh(user_turn)

                started_events = [
                    {"event": "user_turn_saved", "data": {"turn": serialize_turn(user_turn)}},
                    {"event": "agent_started", "data": {"phase": "interviewing"}},
                ]
                if emit:
                    for event in started_events:
                        emit(event)

                session = self.get(db, session_uuid)
                bound_prompt_version = self._bound_interviewer_prompt_version(
                    db,
                    session.id,
                )
                result = self.orchestrator.process(
                    session,
                    user_turn,
                    prompt_version=bound_prompt_version,
                    # The saved answer has already incremented this count.
                    technical_turn_cap_reached=(
                        (session.user_answer_count or 0)
                        >= TECHNICAL_USER_TURN_CAP
                    ),
                )
                assistant_turn = DialogueTurn(
                    # Attach through the relationship, not only the foreign
                    # key.  The orchestrator has already read session.turns to
                    # build the model payload; relationship attachment keeps
                    # that in-memory transcript complete before it is frozen.
                    session=session,
                    turn_index=self._next_turn_index(db, session.id),
                    role="assistant",
                    phase=session.phase,
                    content=result.content,
                    session_action=result.session_action,
                    finish_reason=result.finish_reason,
                    quality_flags=result.quality_flags,
                )
                db.add(assistant_turn)
                db.flush()
                # Persist the linkage before any scoring or stream rendering.
                # If the process stops after this commit, the recovery path
                # above replays this exact answer rather than generating a
                # second one for the same client_turn_id.
                submission.assistant_turn_id = assistant_turn.id
                if (
                    result.prompt_template_id
                    == TECHNICAL_TURN_CAP_ACKNOWLEDGEMENT_PROMPT_ID
                ):
                    trace_action = "technical_turn_cap_acknowledgement"
                elif result.session_action == SUGGEST_FINISH_ACTION:
                    trace_action = "natural_close_suggested"
                else:
                    trace_action = "natural_interview_turn"
                visibility_fallback_used = (
                    "server_interviewer_visibility_fallback_after_leak_exhaustion"
                    in result.quality_flags
                )
                db.add(
                    AgentTrace(
                        session_id=session.id,
                        assistant_turn_id=assistant_turn.id,
                        action=trace_action,
                        model_provider=result.provider,
                        model_name=result.model,
                        prompt_template_id=result.prompt_template_id,
                        prompt_version=result.prompt_version,
                        input_fingerprint=result.input_fingerprint,
                        output_contract={
                            "session_action": result.session_action,
                            "finish_reason": result.finish_reason,
                            "model_session_action": result.model_session_action,
                            "model_finish_reason": result.model_finish_reason,
                            "quality_flags": result.quality_flags,
                            "attempt_count": result.attempt_count,
                            **(
                                {"fallback_used": True}
                                if visibility_fallback_used
                                else {}
                            ),
                            **(
                                {
                                    "model_call_count": 0,
                                    "technical_turn_cap_reached": True,
                                }
                                if trace_action
                                == "technical_turn_cap_acknowledgement"
                                else {}
                            ),
                            **(
                                {"navigation": result.navigation}
                                if result.navigation is not None
                                else {}
                            ),
                        },
                        renderer_status=(
                            "fallback"
                            if visibility_fallback_used
                            else ("repaired" if result.repair_used else "accepted")
                        ),
                        repair_used=result.repair_used,
                        latency_ms=result.latency_ms,
                    )
                )
                if session.phase == "finalizing":
                    self.orchestrator.freeze_transcript(session)
                db.commit()
                session = self.get(db, session_uuid)
                user_turn = db.get(DialogueTurn, user_turn.id)
                assistant_turn = db.get(DialogueTurn, assistant_turn.id)
                if not user_turn or not assistant_turn:
                    raise RuntimeError("persisted_turn_missing_after_processing")
                events = self._completed_events(session, user_turn, assistant_turn)
                submission = db.get(TurnSubmission, submission.id)
                if not submission:
                    raise RuntimeError("submission_missing_after_processing")
                submission.state = "completed"
                submission.response_events = events
                submission.completed_at = utcnow()
                db.commit()
                if emit:
                    # The saved/started prefix was flushed before the model call.
                    # Only deliver the persisted result suffix here.
                    for event in events[len(started_events) :]:
                        emit(event)
                return events
            except ServiceError:
                raise
            except Exception as exc:
                db.rollback()
                failed = db.get(TurnSubmission, submission.id)
                if failed:
                    failed.state = "failed"
                    failed.error_message = f"{type(exc).__name__}: {exc}"[:1000]
                    db.add(
                        TechnicalAnomaly(
                            session_id=failed.session_id,
                            turn_id=failed.user_turn_id,
                            category="interviewer_failure",
                            detail=failed.error_message,
                            recoverable=True,
                        )
                    )
                    failed_session = db.get(AssessmentSession, failed.session_id)
                    if failed_session:
                        failed_session.manual_review_recommended = True
                    if failed.assistant_turn_id:
                        db.add(
                            AgentTrace(
                                session_id=failed.session_id,
                                assistant_turn_id=failed.assistant_turn_id,
                                action="turn_delivery_failure",
                                model_provider="system",
                                model_name="turn-delivery-v6",
                                prompt_template_id="v6_turn_delivery",
                                prompt_version="v6.0.0",
                                input_fingerprint=(
                                    self.orchestrator.trace_input_fingerprint(failed_session)
                                    if failed_session
                                    else None
                                ),
                                output_contract={
                                    "status": "failed",
                                    "exception_type": type(exc).__name__,
                                    "recoverable": True,
                                },
                                renderer_status="failed",
                                fallback_reason=failed.error_message,
                            )
                        )
                    else:
                        failed_prompt_version = self._bound_interviewer_prompt_version(
                            db,
                            failed.session_id,
                        )
                        failed_prompt_id, _, _ = resolve_natural_interviewer_prompt(
                            failed_prompt_version
                        )
                        error_code = str(
                            getattr(exc, "error_code", type(exc).__name__)
                        )
                        attempt_count = int(getattr(exc, "attempt_count", 0) or 0)
                        latency_ms = int(getattr(exc, "latency_ms", 0) or 0)
                        db.add(
                            AgentTrace(
                                session_id=failed.session_id,
                                assistant_turn_id=None,
                                action="natural_interview_turn_failed",
                                model_provider=(
                                    "deepseek"
                                    if settings.model_gateway_mode == "real"
                                    else "mock"
                                ),
                                model_name=(
                                    settings.deepseek_model
                                    if settings.model_gateway_mode == "real"
                                    else "natural-interviewer-mock-v6"
                                ),
                                prompt_template_id=failed_prompt_id,
                                prompt_version=failed_prompt_version,
                                input_fingerprint=(
                                    self.orchestrator.trace_input_fingerprint(failed_session)
                                    if failed_session
                                    else None
                                ),
                                output_contract={
                                    "status": "failed",
                                    "exception_type": type(exc).__name__,
                                    "error_code": error_code,
                                    "attempt_count": attempt_count,
                                    "recoverable": True,
                                },
                                renderer_status="failed",
                                repair_used=bool(getattr(exc, "repair_used", False)),
                                fallback_reason=failed.error_message,
                                latency_ms=latency_ms,
                            )
                        )
                    db.commit()
                raise

    def _claim_evidence_snapshot(
        self,
        db: Session,
        session: AssessmentSession,
        *,
        retry_failed: bool = False,
    ) -> tuple[
        EvidenceReadinessCheck,
        dict[str, Any] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        bool,
    ]:
        fingerprint = transcript_fingerprint(session)
        effective_mode = self._effective_attribution_mode(db, session.id)
        asset_fingerprint = _report_readiness_asset_fingerprint(effective_mode)
        key_filter = (
            EvidenceReadinessCheck.session_id == session.id,
            EvidenceReadinessCheck.transcript_fingerprint == fingerprint,
            EvidenceReadinessCheck.asset_fingerprint == asset_fingerprint,
        )
        existing = db.scalar(select(EvidenceReadinessCheck).where(*key_filter))
        now = utcnow()
        should_run = False
        if existing is not None:
            if existing.status in {"ready", "insufficient"}:
                return existing, None, [], [], False
            if existing.status == "failed" and not retry_failed:
                return existing, None, [], [], False
            if existing.status == "processing":
                current_token = _lease_token(existing.created_at)
                if (
                    existing.model_provider == "queued"
                    and _queued_evidence_task_is_live(existing.id, current_token)
                ):
                    # Executor backlog is not model-runtime. Keep the unique
                    # queued job until its worker atomically starts a fresh
                    # running lease. Missing registrations are orphaned rows
                    # from a restart or failed submit and may be reclaimed now.
                    return existing, None, [], [], False
                created_at = existing.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=now.tzinfo)
                lease_expired = (
                    existing.model_provider == "queued"
                    or created_at
                    < now
                    - timedelta(
                        seconds=_report_readiness_lease_seconds(effective_mode)
                    )
                )
                if not lease_expired:
                    return existing, None, [], [], False
            existing.status = "processing"
            existing.ready = None
            existing.sufficient_dimension_count = None
            existing.model_provider = "queued"
            existing.model_name = "pending"
            existing.repair_used = False
            existing.latency_ms = 0
            existing.attempt_count = 0
            existing.result_data = None
            existing.error = None
            existing.created_at = now
            existing.completed_at = None
            db.execute(
                delete(EvidenceAttributionSpan).where(
                    EvidenceAttributionSpan.readiness_check_id == existing.id
                )
            )
            check = existing
            should_run = True
        else:
            check = EvidenceReadinessCheck(
                session_id=session.id,
                transcript_fingerprint=fingerprint,
                asset_fingerprint=asset_fingerprint,
                status="processing",
                model_provider="queued",
                prompt_template_id=INCREMENTAL_EVIDENCE_PROMPT_ID,
                prompt_version=INCREMENTAL_EVIDENCE_PROMPT_VERSION,
            )
            db.add(check)
            should_run = True

        transcript = _session_transcript_rows(session)
        latest_user_turn_index = max(
            (
                int(item["turn_index"])
                for item in transcript
                if item["role"] == "user"
            ),
            default=None,
        )
        check.last_user_turn_index = latest_user_turn_index
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            concurrent = db.scalar(select(EvidenceReadinessCheck).where(*key_filter))
            if concurrent is None:
                raise
            return concurrent, None, [], [], False
        db.refresh(check)

        previous = db.scalar(
            select(EvidenceReadinessCheck)
            .where(
                EvidenceReadinessCheck.session_id == session.id,
                EvidenceReadinessCheck.asset_fingerprint == asset_fingerprint,
                EvidenceReadinessCheck.id != check.id,
                EvidenceReadinessCheck.status.in_({"ready", "insufficient"}),
                EvidenceReadinessCheck.result_data.is_not(None),
            )
            .order_by(
                EvidenceReadinessCheck.last_user_turn_index.desc(),
                EvidenceReadinessCheck.completed_at.desc(),
            )
            .limit(1)
        )
        previous_index = (
            previous.last_user_turn_index
            if previous is not None and previous.last_user_turn_index is not None
            else -1
        )
        new_user_turns = [
            item
            for item in transcript
            if item["role"] == "user" and int(item["turn_index"]) > previous_index
        ]
        previous_snapshot = (
            dict(previous.result_data)
            if previous is not None and previous.result_data is not None
            else None
        )
        return check, previous_snapshot, new_user_turns, transcript, should_run

    def _execute_evidence_snapshot(
        self,
        session_factory: Callable[[], Session],
        check_id: int,
        previous_snapshot: dict[str, Any] | None,
        new_user_turns: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
        expected_lease_token: str | None = None,
    ) -> None:
        started = time.monotonic()
        stage = "legacy_incremental_scoring"
        failure_trace_recorded = False

        def lease_is_current(check: EvidenceReadinessCheck | None) -> bool:
            if check is None or check.status != "processing":
                return False
            if expected_lease_token is None:
                return True
            return _lease_token(check.created_at) == expected_lease_token

        try:
            with session_factory() as db:
                expected_lease_token = _start_evidence_snapshot_lease(
                    db,
                    check_id,
                    expected_lease_token,
                )
                if expected_lease_token is None:
                    return
                check = db.get(EvidenceReadinessCheck, check_id)
                if not lease_is_current(check):
                    return
                assert check is not None
                effective_mode = self._effective_attribution_mode(
                    db, check.session_id
                )
                expected_transcript_fingerprint = check.transcript_fingerprint
                expected_asset_fingerprint = check.asset_fingerprint

            attributed_assessment = None
            attribution_error: Exception | None = None
            if effective_mode in {"shadow", "enforce"}:
                try:
                    stage = "evidence_attribution"
                    attribution = self.orchestrator.assess_evidence_attribution(
                        transcript=transcript
                    )
                    with session_factory() as db:
                        check = db.get(EvidenceReadinessCheck, check_id)
                        if not lease_is_current(check):
                            return
                        assert check is not None
                        user_turns = {
                            turn.turn_index: turn
                            for turn in db.scalars(
                                select(DialogueTurn).where(
                                    DialogueTurn.session_id == check.session_id,
                                    DialogueTurn.role == "user",
                                )
                            )
                        }
                        db.execute(
                            delete(EvidenceAttributionSpan).where(
                                EvidenceAttributionSpan.readiness_check_id == check.id
                            )
                        )
                        for validated in attribution.spans:
                            source = user_turns.get(validated.output.turn_index)
                            if source is None:
                                raise FinalizationError(
                                    "attribution_user_turn_missing_during_persist"
                                )
                            db.add(
                                EvidenceAttributionSpan(
                                    session_id=check.session_id,
                                    user_turn_id=source.id,
                                    readiness_check_id=check.id,
                                    turn_index=validated.output.turn_index,
                                    quote=validated.output.quote,
                                    start=validated.output.start,
                                    end=validated.output.end,
                                    text_hash=validated.text_hash,
                                    owner=validated.output.owner,
                                    relation=validated.output.relation,
                                    elicitation_level=validated.output.elicitation_level,
                                    source_label=validated.output.source_label,
                                    confidence=validated.output.confidence,
                                    reason=validated.output.reason,
                                    eligibility=validated.eligibility,
                                    validation_status=validated.validation_status,
                                    validation_reason=validated.validation_reason,
                                    transcript_fingerprint=check.transcript_fingerprint,
                                    asset_fingerprint=check.asset_fingerprint,
                                    prompt_template_id=attribution.prompt_template_id,
                                    prompt_version=attribution.prompt_version,
                                    schema_version=attribution.schema_version,
                                )
                            )
                        db.flush()
                        eligibility_counts: dict[str, int] = defaultdict(int)
                        for span in check.attribution_spans:
                            eligibility_counts[span.eligibility] += 1
                        db.add(
                            AgentTrace(
                                session_id=check.session_id,
                                assistant_turn_id=None,
                                action="evidence_attribution_snapshot",
                                model_provider=attribution.provider,
                                model_name=attribution.model,
                                prompt_template_id=attribution.prompt_template_id,
                                prompt_version=attribution.prompt_version,
                                input_fingerprint=attribution.input_fingerprint,
                                output_contract={
                                    "status": "validated",
                                    "readiness_check_id": check.id,
                                    "transcript_fingerprint": check.transcript_fingerprint,
                                    "asset_fingerprint": check.asset_fingerprint,
                                    "schema_version": attribution.schema_version,
                                    "candidate_rule_version": EVIDENCE_CANDIDATE_RULE_VERSION,
                                    "eligibility_rule_version": EVIDENCE_ELIGIBILITY_RULE_VERSION,
                                    "span_count": len(attribution.spans),
                                    "eligibility_counts": dict(eligibility_counts),
                                    "attempt_count": attribution.attempt_count,
                                },
                                renderer_status=(
                                    "repaired"
                                    if attribution.repair_used
                                    else "accepted"
                                ),
                                repair_used=attribution.repair_used,
                                latency_ms=attribution.latency_ms,
                            )
                        )
                        db.commit()

                    stage = "attributed_span_scoring"
                    with session_factory() as db:
                        check = db.get(EvidenceReadinessCheck, check_id)
                        if not lease_is_current(check):
                            return
                        eligible_spans = list(
                            db.scalars(
                                select(EvidenceAttributionSpan)
                                .where(
                                    EvidenceAttributionSpan.readiness_check_id
                                    == check_id,
                                    EvidenceAttributionSpan.eligibility == "eligible",
                                    EvidenceAttributionSpan.validation_status
                                    == "validated",
                                )
                                .order_by(
                                    EvidenceAttributionSpan.turn_index,
                                    EvidenceAttributionSpan.start,
                                )
                            )
                        )
                        attributed_assessment = (
                            self.orchestrator.assess_attributed_evidence(
                                eligible_spans=eligible_spans,
                                transcript=transcript,
                                expected_check_id=check_id,
                                expected_transcript_fingerprint=(
                                    expected_transcript_fingerprint
                                ),
                                expected_asset_fingerprint=expected_asset_fingerprint,
                            )
                        )
                        db.add(
                            AgentTrace(
                                session_id=check.session_id,
                                assistant_turn_id=None,
                                action=(
                                    "attributed_evidence_snapshot_shadow"
                                    if effective_mode == "shadow"
                                    else "attributed_evidence_snapshot"
                                ),
                                model_provider=attributed_assessment.provider,
                                model_name=attributed_assessment.model,
                                prompt_template_id=(
                                    attributed_assessment.prompt_template_id
                                ),
                                prompt_version=attributed_assessment.prompt_version,
                                input_fingerprint=check.transcript_fingerprint,
                                output_contract={
                                    "status": "shadow" if effective_mode == "shadow" else "enforced",
                                    "readiness_check_id": check.id,
                                    "schema_version": ATTRIBUTED_EVIDENCE_SCHEMA_VERSION,
                                    "ready": attributed_assessment.ready,
                                    "sufficient_dimension_count": (
                                        attributed_assessment.sufficient_dimension_count
                                    ),
                                    "attempt_count": attributed_assessment.attempt_count,
                                    "result": attributed_assessment.output.model_dump(
                                        mode="json"
                                    ),
                                },
                                renderer_status=(
                                    "repaired"
                                    if attributed_assessment.repair_used
                                    else "accepted"
                                ),
                                repair_used=attributed_assessment.repair_used,
                                latency_ms=attributed_assessment.latency_ms,
                            )
                        )
                        db.commit()
                except Exception as exc:
                    attribution_error = exc
                    with session_factory() as db:
                        check = db.get(EvidenceReadinessCheck, check_id)
                        if not lease_is_current(check):
                            return
                        assert check is not None
                        db.add(
                            AgentTrace(
                                session_id=check.session_id,
                                assistant_turn_id=None,
                                action=f"{stage}_failed",
                                model_provider=(
                                    "deepseek"
                                    if settings.model_gateway_mode == "real"
                                    else "mock"
                                ),
                                model_name=(
                                    settings.deepseek_model
                                    if settings.model_gateway_mode == "real"
                                    else stage + "-mock-v6"
                                ),
                                prompt_template_id=(
                                    EVIDENCE_ATTRIBUTION_PROMPT_ID
                                    if stage == "evidence_attribution"
                                    else ATTRIBUTED_EVIDENCE_PROMPT_ID
                                ),
                                prompt_version=(
                                    EVIDENCE_ATTRIBUTION_PROMPT_VERSION
                                    if stage == "evidence_attribution"
                                    else ATTRIBUTED_EVIDENCE_PROMPT_VERSION
                                ),
                                input_fingerprint=check.transcript_fingerprint,
                                output_contract={
                                    "status": "failed",
                                    "mode": effective_mode,
                                    "readiness_check_id": check.id,
                                    "schema_version": (
                                        EVIDENCE_ATTRIBUTION_SCHEMA_VERSION
                                        if stage == "evidence_attribution"
                                        else ATTRIBUTED_EVIDENCE_SCHEMA_VERSION
                                    ),
                                    **(
                                        {
                                            "candidate_rule_version": EVIDENCE_CANDIDATE_RULE_VERSION
                                        }
                                        if stage == "evidence_attribution"
                                        else {}
                                    ),
                                    "error_code": str(
                                        getattr(exc, "error_code", type(exc).__name__)
                                    ),
                                    "attempt_count": int(
                                        getattr(exc, "attempt_count", 0) or 0
                                    ),
                                },
                                renderer_status="failed",
                                repair_used=bool(
                                    getattr(exc, "repair_used", False)
                                ),
                                fallback_used=effective_mode == "shadow",
                                fallback_reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                                latency_ms=int(
                                    getattr(exc, "latency_ms", 0) or 0
                                ),
                            )
                        )
                        db.commit()
                        failure_trace_recorded = True
                    if effective_mode == "enforce":
                        raise

            if effective_mode == "enforce":
                if attributed_assessment is None:
                    raise FinalizationError(
                        "enforced_attribution_result_missing"
                    ) from attribution_error
                assessment = attributed_assessment
                result_action = "attributed_evidence_snapshot_committed"
            else:
                stage = "legacy_incremental_scoring"
                assessment = self.orchestrator.assess_incremental_evidence(
                    previous_snapshot=previous_snapshot,
                    new_user_turns=new_user_turns,
                    transcript=transcript,
                )
                result_action = "incremental_evidence_snapshot"

            with session_factory() as db:
                check = db.get(EvidenceReadinessCheck, check_id)
                if not lease_is_current(check):
                    return
                assert check is not None
                check.status = "ready" if assessment.ready else "insufficient"
                check.ready = assessment.ready
                check.sufficient_dimension_count = assessment.sufficient_dimension_count
                check.model_provider = assessment.provider
                check.model_name = assessment.model
                check.prompt_template_id = assessment.prompt_template_id
                check.prompt_version = assessment.prompt_version
                check.repair_used = assessment.repair_used
                check.latency_ms = assessment.latency_ms
                check.attempt_count = assessment.attempt_count
                check.result_data = assessment.output.model_dump(mode="json")
                check.completed_at = utcnow()
                db.add(
                    AgentTrace(
                        session_id=check.session_id,
                        assistant_turn_id=None,
                        action=result_action,
                        model_provider=assessment.provider,
                        model_name=assessment.model,
                        prompt_template_id=assessment.prompt_template_id,
                        prompt_version=assessment.prompt_version,
                        input_fingerprint=check.transcript_fingerprint,
                        output_contract={
                            "status": check.status,
                            "ready": assessment.ready,
                            "evidence_ready": assessment.evidence_ready,
                            "sufficient_dimension_count": assessment.sufficient_dimension_count,
                            "minimum_turns_required": assessment.minimum_turns_required,
                            "minimum_turns_met": assessment.minimum_turns_met,
                            "last_user_turn_index": check.last_user_turn_index,
                            "attempt_count": assessment.attempt_count,
                            "evidence_attribution_mode": effective_mode,
                            **(
                                {
                                    "schema_version": (
                                        ATTRIBUTED_EVIDENCE_SCHEMA_VERSION
                                    )
                                }
                                if effective_mode == "enforce"
                                else {}
                            ),
                        },
                        renderer_status="repaired" if assessment.repair_used else "accepted",
                        repair_used=assessment.repair_used,
                        latency_ms=assessment.latency_ms,
                    )
                )
                db.commit()
        except Exception as exc:
            with session_factory() as db:
                check = db.get(EvidenceReadinessCheck, check_id)
                if not lease_is_current(check):
                    return
                assert check is not None
                check.status = "failed"
                check.ready = None
                check.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                elapsed_ms = max(1, int((time.monotonic() - started) * 1000))
                check.latency_ms = max(
                    elapsed_ms,
                    int(getattr(exc, "latency_ms", 0) or 0),
                )
                check.attempt_count = max(
                    1,
                    int(getattr(exc, "attempt_count", 0) or 0),
                )
                check.completed_at = utcnow()
                if (
                    not failure_trace_recorded
                    or stage == "legacy_incremental_scoring"
                ):
                    db.add(
                        AgentTrace(
                            session_id=check.session_id,
                            assistant_turn_id=None,
                            action=f"{stage}_failed",
                            model_provider=(
                                "deepseek"
                                if settings.model_gateway_mode == "real"
                                else "mock"
                            ),
                            model_name=(
                                settings.deepseek_model
                                if settings.model_gateway_mode == "real"
                                else "natural-incremental-evidence-mock-v6"
                            ),
                            prompt_template_id=(
                                EVIDENCE_ATTRIBUTION_PROMPT_ID
                                if stage == "evidence_attribution"
                                else (
                                    ATTRIBUTED_EVIDENCE_PROMPT_ID
                                    if stage == "attributed_span_scoring"
                                    else INCREMENTAL_EVIDENCE_PROMPT_ID
                                )
                            ),
                            prompt_version=(
                                EVIDENCE_ATTRIBUTION_PROMPT_VERSION
                                if stage == "evidence_attribution"
                                else (
                                    ATTRIBUTED_EVIDENCE_PROMPT_VERSION
                                    if stage == "attributed_span_scoring"
                                    else INCREMENTAL_EVIDENCE_PROMPT_VERSION
                                )
                            ),
                            input_fingerprint=check.transcript_fingerprint,
                            output_contract={
                                "status": "failed",
                                **(
                                    {
                                        "schema_version": (
                                            EVIDENCE_ATTRIBUTION_SCHEMA_VERSION
                                            if stage == "evidence_attribution"
                                            else ATTRIBUTED_EVIDENCE_SCHEMA_VERSION
                                        )
                                    }
                                    if stage
                                    in {
                                        "evidence_attribution",
                                        "attributed_span_scoring",
                                    }
                                    else {}
                                ),
                                "error_code": str(
                                    getattr(exc, "error_code", type(exc).__name__)
                                ),
                                "attempt_count": check.attempt_count,
                            },
                            renderer_status="failed",
                            fallback_reason=check.error,
                            latency_ms=check.latency_ms,
                        )
                    )
                db.commit()

    def schedule_evidence_snapshot(
        self,
        session_factory: Callable[[], Session],
        session_uuid: str,
        *,
        retry_failed: bool = False,
    ) -> None:
        if not settings.evidence_observer_enabled:
            return
        queued_lease_token: str | None = None
        with session_factory() as db:
            with _locks[session_uuid]:
                session = self.get(db, session_uuid)
                if session.phase != "interviewing":
                    return
                check, previous, new_turns, transcript, should_run = (
                    self._claim_evidence_snapshot(
                        db,
                        session,
                        retry_failed=retry_failed,
                    )
                )
                if should_run:
                    queued_lease_token = _lease_token(check.created_at)
                    _register_queued_evidence_task(check.id, queued_lease_token)
        if not should_run:
            return
        assert queued_lease_token is not None
        args = (
            session_factory,
            check.id,
            previous,
            new_turns,
            transcript,
            queued_lease_token,
        )
        try:
            if settings.model_gateway_mode == "mock":
                self._execute_evidence_snapshot(*args)
            else:
                _evidence_executor.submit(self._execute_evidence_snapshot, *args)
        except Exception:
            _discard_queued_evidence_task(check.id, queued_lease_token)
            raise

    def report_readiness(
        self,
        db: Session,
        session_uuid: str,
        session_factory: Callable[[], Session],
        *,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        """Return the exact transcript's non-blocking evidence snapshot state."""

        self.schedule_evidence_snapshot(
            session_factory,
            session_uuid,
            retry_failed=retry_failed,
        )
        db.expire_all()
        session = self.get(db, session_uuid)
        if session.phase != "interviewing":
            raise ServiceError(
                409,
                "session_not_open_for_readiness_check",
                "当前会话不需要再次检查报告准备度。",
            )
        effective_mode = self._effective_attribution_mode(db, session.id)
        check = db.scalar(
            select(EvidenceReadinessCheck).where(
                EvidenceReadinessCheck.session_id == session.id,
                EvidenceReadinessCheck.transcript_fingerprint
                == transcript_fingerprint(session),
                EvidenceReadinessCheck.asset_fingerprint
                == _report_readiness_asset_fingerprint(effective_mode),
            )
        )
        if check is None:
            raise ServiceError(
                503,
                "evidence_snapshot_unavailable",
                "当前证据结果尚未整理完成，请稍后重试。",
            )
        return serialize_report_readiness(
            check,
            cached=check.status in {"ready", "insufficient"},
            saved_user_answer_count=session.user_answer_count,
        )

    def _run_finalization(
        self,
        db: Session,
        session: AssessmentSession,
        check: EvidenceReadinessCheck | None = None,
    ) -> AssessmentSession:
        try:
            precomputed: StructuredCallResult[
                FinalScorerOutput | AttributedFinalScorerOutput
            ] | None = None
            attribution_spans: list[EvidenceAttributionSpan] | None = None
            if check is not None:
                if check.result_data is None:
                    raise FinalizationError("evidence_snapshot_result_missing")
                dimensions = check.result_data.get("dimensions")
                is_attributed = bool(
                    isinstance(dimensions, list)
                    and dimensions
                    and isinstance(dimensions[0], dict)
                    and "evidence_refs" in dimensions[0]
                )
                output: FinalScorerOutput | AttributedFinalScorerOutput
                if is_attributed:
                    output = AttributedFinalScorerOutput.model_validate(
                        check.result_data
                    )
                    attribution_spans = list(
                        db.scalars(
                            select(EvidenceAttributionSpan)
                            .where(
                                EvidenceAttributionSpan.readiness_check_id
                                == check.id
                            )
                            .order_by(EvidenceAttributionSpan.id)
                        )
                    )
                else:
                    output = FinalScorerOutput.model_validate(check.result_data)
                precomputed = StructuredCallResult(
                    output=output,
                    provider=check.model_provider,
                    model=check.model_name,
                    repair_used=check.repair_used,
                    latency_ms=check.latency_ms,
                    attempt_count=max(1, check.attempt_count),
                )
            return self.orchestrator.finalize(
                db,
                session,
                precomputed=precomputed,
                precomputed_prompt_template_id=(
                    check.prompt_template_id if check is not None else None
                ),
                precomputed_prompt_version=(
                    check.prompt_version if check is not None else None
                ),
                attribution_spans=attribution_spans,
                readiness_check_id=(
                    check.id if attribution_spans is not None and check is not None else None
                ),
                expected_asset_fingerprint=(
                    check.asset_fingerprint
                    if attribution_spans is not None and check is not None
                    else None
                ),
            )
        except FinalizationError as exc:
            raise ServiceError(
                503,
                "scoring_failed",
                "独立评分暂时失败；访谈已冻结，请稍后重试生成报告。",
            ) from exc

    @staticmethod
    def _stale_closure_suggestion() -> ServiceError:
        return ServiceError(
            409,
            "stale_closure_suggestion",
            "该收束建议已不是当前对话的最新状态，请刷新后再决定。",
        )

    def accept_closure_suggestion(
        self,
        db: Session,
        session_uuid: str,
        closure_turn_id: int,
        expected_transcript_fingerprint: str,
    ) -> AssessmentSession:
        """Accept only the latest persisted natural-close suggestion.

        The turn id and fingerprint form an optimistic concurrency boundary for
        refreshes and multiple browser tabs.  Retrying an already accepted
        suggestion remains idempotent and can also retry a failed frozen score.
        """

        with _locks[session_uuid]:
            session = self.get(db, session_uuid)
            closure_turn = db.scalar(
                select(DialogueTurn).where(
                    DialogueTurn.id == closure_turn_id,
                    DialogueTurn.session_id == session.id,
                    DialogueTurn.role == "assistant",
                    DialogueTurn.session_action == SUGGEST_FINISH_ACTION,
                )
            )
            if closure_turn is None:
                raise self._stale_closure_suggestion()

            accepted_trace = db.scalar(
                select(AgentTrace).where(
                    AgentTrace.session_id == session.id,
                    AgentTrace.assistant_turn_id == closure_turn.id,
                    AgentTrace.action == "user_accepted_closure_suggestion",
                )
            )
            if accepted_trace is not None:
                if (
                    accepted_trace.input_fingerprint
                    != expected_transcript_fingerprint
                    or session.transcript_fingerprint
                    != expected_transcript_fingerprint
                ):
                    raise self._stale_closure_suggestion()
                if session.report and session.phase == "completed":
                    return session
                if session.phase != "finalizing":
                    raise self._stale_closure_suggestion()
                return self._run_finalization(db, session)

            suggestion = active_closure_suggestion(session)
            if (
                suggestion is None
                or suggestion["closure_turn_id"] != closure_turn.id
                or suggestion["transcript_fingerprint"]
                != expected_transcript_fingerprint
            ):
                raise self._stale_closure_suggestion()

            session.phase = "finalizing"
            session.finalization_state = "user_accepted_close"
            frozen_fingerprint = self.orchestrator.freeze_transcript(session)
            if frozen_fingerprint != expected_transcript_fingerprint:
                db.rollback()
                raise self._stale_closure_suggestion()
            db.add(
                AgentTrace(
                    session_id=session.id,
                    assistant_turn_id=closure_turn.id,
                    action="user_accepted_closure_suggestion",
                    model_provider="none",
                    model_name="none",
                    prompt_template_id="v6_closure_confirmation",
                    prompt_version="v6.0.0",
                    input_fingerprint=frozen_fingerprint,
                    output_contract={
                        "session_action": "finish",
                        "finish_reason": "user_requested",
                        "source_session_action": SUGGEST_FINISH_ACTION,
                        "source_finish_reason": closure_turn.finish_reason,
                        "closure_turn_id": closure_turn.id,
                        "transcript_fingerprint": frozen_fingerprint,
                    },
                    renderer_status="accepted",
                )
            )
            db.commit()
            db.refresh(session)
            return self._run_finalization(db, session)

    def finalize(
        self,
        db: Session,
        session_uuid: str,
        request: FinalizeSessionRequest | None = None,
    ) -> AssessmentSession:
        request = request or FinalizeSessionRequest()
        with _locks[session_uuid]:
            session = self.get(db, session_uuid)
            if session.report and session.phase == "completed":
                return session
            check: EvidenceReadinessCheck | None = None
            effective_mode = self._effective_attribution_mode(db, session.id)
            current_asset_fingerprint = _report_readiness_asset_fingerprint(
                effective_mode
            )
            if session.phase == "interviewing":
                current_fingerprint = transcript_fingerprint(session)
                if request.evidence_check_id is not None:
                    check = db.get(
                        EvidenceReadinessCheck,
                        request.evidence_check_id,
                    )
                else:
                    check = db.scalar(
                        select(EvidenceReadinessCheck).where(
                            EvidenceReadinessCheck.session_id == session.id,
                            EvidenceReadinessCheck.transcript_fingerprint
                            == current_fingerprint,
                            EvidenceReadinessCheck.asset_fingerprint
                            == current_asset_fingerprint,
                        )
                    )
                if (
                    check is None
                    or check.session_id != session.id
                    or check.transcript_fingerprint != current_fingerprint
                    or check.asset_fingerprint != current_asset_fingerprint
                    or (
                        request.expected_transcript_fingerprint is not None
                        and request.expected_transcript_fingerprint
                        != current_fingerprint
                    )
                ):
                    raise ServiceError(
                        409,
                        "stale_evidence_snapshot",
                        "对话已更新，请等待当前回答的证据结果后再提交。",
                    )
                if check.status == "processing":
                    raise ServiceError(
                        409,
                        "evidence_snapshot_processing",
                        "正在整理最新一轮的证据，完成后即可生成报告。",
                    )
                if check.status == "failed" or check.result_data is None:
                    raise ServiceError(
                        503,
                        "evidence_snapshot_failed",
                        "当前证据结果暂未整理完成，请重试；会话不会被冻结。",
                    )
                if check.status == "insufficient" and not request.allow_incomplete:
                    raise ServiceError(
                        409,
                        "evidence_insufficient",
                        "现有回答尚不足以支持完整报告；确认后可生成证据有限的报告。",
                    )
                session.ended_early = (
                    session.user_answer_count
                    < settings.natural_interview_min_user_turns
                )
                session.phase = "finalizing"
                session.finalization_state = "user_requested"
                frozen_fingerprint = self.orchestrator.freeze_transcript(session)
                db.add(
                    AgentTrace(
                        session_id=session.id,
                        assistant_turn_id=None,
                        action="user_requested_finalize",
                        model_provider="none",
                        model_name="none",
                        prompt_template_id="v6_user_finalize",
                        prompt_version="v6.0.0",
                        input_fingerprint=frozen_fingerprint,
                        output_contract={
                            "session_action": "finish",
                            "finish_reason": "user_requested",
                            "evidence_check_id": check.id,
                            "evidence_status": check.status,
                            "allow_incomplete": request.allow_incomplete,
                            "ended_early": session.ended_early,
                            "saved_user_answer_count": session.user_answer_count,
                            "minimum_turns_required": settings.natural_interview_min_user_turns,
                            "transcript_fingerprint": frozen_fingerprint,
                        },
                        renderer_status="accepted",
                    )
                )
                db.commit()
            elif session.phase == "finalizing":
                frozen_fingerprint = session.transcript_fingerprint
                if frozen_fingerprint:
                    check = db.scalar(
                        select(EvidenceReadinessCheck).where(
                            EvidenceReadinessCheck.session_id == session.id,
                            EvidenceReadinessCheck.transcript_fingerprint
                            == frozen_fingerprint,
                            EvidenceReadinessCheck.asset_fingerprint
                            == current_asset_fingerprint,
                            EvidenceReadinessCheck.status.in_({"ready", "insufficient"}),
                            EvidenceReadinessCheck.result_data.is_not(None),
                        )
                    )
                bound_version = self._bound_interviewer_prompt_version(db, session.id)
                if (
                    check is None
                    and bound_version in EVIDENCE_GATED_INTERVIEWER_PROMPT_VERSIONS
                ):
                    raise ServiceError(
                        503,
                        "evidence_snapshot_failed",
                        "已冻结的对话缺少对应证据快照，请联系管理员恢复。",
                    )
            else:
                raise ServiceError(
                    409,
                    "session_not_ready_for_finalization",
                    "当前会话不能生成报告。",
                )
            return self._run_finalization(db, session, check)

    def exit(self, db: Session, session_uuid: str, reason: str | None) -> AssessmentSession:
        with _locks[session_uuid]:
            session = self.get(db, session_uuid)
            if session.phase == "completed":
                return session
            session.phase = "exited"
            session.ended_early = True
            profile = dict(session.profile_data or {})
            if reason:
                profile["exit_reason"] = reason
            session.profile_data = profile
            db.commit()
            db.refresh(session)
            return session
