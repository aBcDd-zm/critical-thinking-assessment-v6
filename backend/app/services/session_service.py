"""Session lifecycle for the V6 natural interview."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AgentTrace,
    AssessmentSession,
    DialogueTurn,
    HumanReview,
    TechnicalAnomaly,
    TurnSubmission,
    utcnow,
)
from app.schemas import (
    MIN_ANSWER_VISIBLE_CHARACTERS,
    CreateSessionRequest,
    SubmitTurnRequest,
    is_explicit_uncertainty_answer,
    visible_character_count,
)
from app.services.model_gateway import (
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    ModelGatewayError,
)
from app.services.orchestrator import (
    FinalizationError,
    InterviewContractError,
    InterviewOrchestrator,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
TECHNICAL_USER_TURN_CAP = 40


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
    data = dict(session.report.report_data)
    data["generated_at"] = session.report.created_at.isoformat()
    return data


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
            opening = self.orchestrator.opening(participant_payload)
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
        self, db: Session, session_uuid: str, request: SubmitTurnRequest
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

                session = self.get(db, session_uuid)
                result = self.orchestrator.process(session, user_turn)
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
                db.add(
                    AgentTrace(
                        session_id=session.id,
                        assistant_turn_id=assistant_turn.id,
                        action="natural_interview_turn",
                        model_provider=result.provider,
                        model_name=result.model,
                        prompt_template_id=result.prompt_template_id,
                        prompt_version=result.prompt_version,
                        input_fingerprint=result.input_fingerprint,
                        output_contract={
                            "session_action": result.session_action,
                            "finish_reason": result.finish_reason,
                            "quality_flags": result.quality_flags,
                        },
                        renderer_status="repaired" if result.repair_used else "accepted",
                        repair_used=result.repair_used,
                        latency_ms=result.latency_ms,
                    )
                )
                if session.phase == "finalizing":
                    self.orchestrator.freeze_transcript(session)
                db.commit()

                # A model-led natural close should attempt independent scoring
                # immediately. If scoring is unavailable, the frozen session
                # remains in finalizing and POST /finalize is the idempotent retry.
                if session.phase == "finalizing":
                    try:
                        self.orchestrator.finalize(db, session)
                    except FinalizationError:
                        pass
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
                                prompt_template_id=NATURAL_INTERVIEWER_PROMPT_ID,
                                prompt_version=NATURAL_INTERVIEWER_PROMPT_VERSION,
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
                                repair_used=bool(getattr(exc, "repair_used", False)),
                                fallback_reason=failed.error_message,
                            )
                        )
                    db.commit()
                raise

    def finalize(self, db: Session, session_uuid: str) -> AssessmentSession:
        with _locks[session_uuid]:
            session = self.get(db, session_uuid)
            if session.report and session.phase == "completed":
                return session
            if session.phase == "interviewing":
                # This is the explicit user-controlled end, not an interviewer
                # fallback. No synthetic question or scripted closing is added.
                session.phase = "finalizing"
                session.finalization_state = "user_requested"
                self.orchestrator.freeze_transcript(session)
                db.add(
                    AgentTrace(
                        session_id=session.id,
                        assistant_turn_id=None,
                        action="user_requested_finalize",
                        model_provider="none",
                        model_name="none",
                        prompt_template_id="v6_user_finalize",
                        prompt_version="v6.0.0",
                        input_fingerprint=self.orchestrator.trace_input_fingerprint(session),
                        output_contract={"session_action": "finish", "finish_reason": "user_requested"},
                        renderer_status="accepted",
                    )
                )
                db.commit()
            elif session.phase != "finalizing":
                raise ServiceError(
                    409,
                    "session_not_ready_for_finalization",
                    "当前会话不能生成报告。",
                )
            try:
                return self.orchestrator.finalize(db, session)
            except FinalizationError as exc:
                raise ServiceError(
                    503,
                    "scoring_failed",
                    "独立评分暂时失败；访谈已冻结，请稍后重试生成报告。",
                ) from exc

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
