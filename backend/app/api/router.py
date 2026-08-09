"""HTTP API for the V6 natural-interview demo."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

import jwt
from argon2 import PasswordHasher
from fastapi import APIRouter, Body, Cookie, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db, get_session_factory
from app.domain.catalog import DIMENSION_BY_KEY
from app.models import (
    AgentTrace,
    AssessmentReport,
    AssessmentSession,
    DialogueTurn,
    EvidenceItem,
    ExpertScore,
    HumanReview,
    ScoringRun,
    TechnicalAnomaly,
    utcnow,
)
from app.schemas import (
    AcceptClosureSuggestionRequest,
    AdminLoginRequest,
    CreateSessionRequest,
    ExitRequest,
    ExpertScoresRequest,
    FinalizeSessionRequest,
    ReviewRequest,
    SubmitTurnRequest,
)
from app.services.model_gateway import ModelGatewayError
from app.services.session_service import (
    ServiceError,
    SessionService,
    serialize_report,
    session_snapshot,
)
from app.services.tts_service import PersistedAITurn, TTSService


router = APIRouter()
sessions = SessionService()

_PUBLIC_DIMENSION_SUGGESTIONS = {
    "problem_definition": "继续明确目标、范围和需要核实的边界。",
    "evidence_evaluation": "继续核实来源、样本范围和仍不确定的信息。",
    "reasoning_argumentation": "继续区分结论、依据与可能改变结论的假设。",
    "multiple_perspectives": "继续补充不同相关方和选择可能带来的影响。",
    "integrative_decision": "继续把目标、约束、风险和回退条件放在一起权衡。",
    "dynamic_adjustment": "继续提前写下会触发调整的信号和下一步行动。",
}

_PUBLIC_LIMITED_EVIDENCE_NOTICE = (
    "“证据有限”或“未充分测得”只表示系统尚未从本次对话中找到足够、"
    "可核验的原话来支持该维度出分；这不等于低分、能力不足或回答质量不高，"
    "也不会按 0 分计入综合总分。该标记不会单独决定是否付酬；"
    "报酬仍按活动参与规则核对。"
)

_ADMIN_SESSION_COOKIE = "admin_session"
_ADMIN_CSRF_COOKIE = "cta_v6_admin_csrf"
_ADMIN_SESSION_COOKIE_PATH = "/api/v1/admin"
_ADMIN_CSRF_COOKIE_PATH = "/"
_ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60
_CSRF_HEADER_NAME = "X-CSRF-Token"
_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class AdminIdentity:
    username: str
    display_name: str


def _service_error(exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


def _admin_auth_configuration() -> tuple[str, str, str]:
    """Return the configured single-admin credentials or fail closed."""

    username = settings.admin_username.strip()
    password_hash = settings.admin_password_hash.strip()
    jwt_secret = settings.admin_jwt_secret.strip()
    if (
        not username
        or not password_hash.startswith("$argon2id$")
        or len(jwt_secret) < 32
    ):
        raise HTTPException(status_code=503, detail={"code": "admin_auth_unavailable"})
    return username, password_hash, jwt_secret


def _admin_unauthorized() -> None:
    raise HTTPException(status_code=401, detail={"code": "admin_unauthorized"})


def _require_admin(
    admin_session: Optional[str] = Cookie(default=None, alias=_ADMIN_SESSION_COOKIE),
) -> AdminIdentity:
    username, _password_hash, jwt_secret = _admin_auth_configuration()
    if not admin_session:
        _admin_unauthorized()
    try:
        claims = jwt.decode(
            admin_session,
            jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        _admin_unauthorized()

    token_username = claims.get("sub")
    display_name = claims.get("display_name")
    if (
        claims.get("scope") != "v6_admin"
        or not isinstance(token_username, str)
        or token_username != username
        or not isinstance(display_name, str)
        or not display_name.strip()
    ):
        _admin_unauthorized()
    return AdminIdentity(username=username, display_name=display_name.strip())


def _require_csrf(
    x_csrf_token: Optional[str] = Header(default=None, alias=_CSRF_HEADER_NAME),
    admin_csrf: Optional[str] = Cookie(default=None, alias=_ADMIN_CSRF_COOKIE),
) -> None:
    if not x_csrf_token or not admin_csrf or not secrets.compare_digest(x_csrf_token, admin_csrf):
        raise HTTPException(status_code=403, detail={"code": "admin_csrf_invalid"})


def _set_admin_cookies(response: Response, *, session_token: str, csrf_token: str) -> None:
    secure = settings.app_env == "production"
    response.set_cookie(
        key=_ADMIN_SESSION_COOKIE,
        value=session_token,
        max_age=_ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=_ADMIN_SESSION_COOKIE_PATH,
    )
    response.set_cookie(
        key=_ADMIN_CSRF_COOKIE,
        value=csrf_token,
        max_age=_ADMIN_SESSION_TTL_SECONDS,
        httponly=False,
        secure=secure,
        samesite="strict",
        # JavaScript running at /admin needs to read this double-submit token.
        # The authenticated session itself remains restricted to management API
        # paths above.
        path=_ADMIN_CSRF_COOKIE_PATH,
    )


def _clear_admin_cookies(response: Response) -> None:
    secure = settings.app_env == "production"
    for cookie_name, httponly, path in (
        (_ADMIN_SESSION_COOKIE, True, _ADMIN_SESSION_COOKIE_PATH),
        (_ADMIN_CSRF_COOKIE, False, _ADMIN_CSRF_COOKIE_PATH),
    ):
        response.delete_cookie(
            key=cookie_name,
            path=path,
            secure=secure,
            httponly=httponly,
            samesite="strict",
        )


def _effective_reviewer(requested_reviewer: Optional[str], identity: AdminIdentity) -> str:
    """Keep explicit historical/external labels while defaulting manual work safely."""

    return (requested_reviewer or "").strip() or identity.display_name


@router.post("/admin/auth/login")
def admin_login(request: AdminLoginRequest, response: Response) -> dict[str, Any]:
    username, password_hash, jwt_secret = _admin_auth_configuration()
    try:
        password_valid = _password_hasher.verify(password_hash, request.password)
    except Exception:
        # Treat malformed or mismatched values identically so this endpoint
        # cannot be used as a credential/configuration oracle.
        password_valid = False
    if request.username.strip() != username or not password_valid:
        _admin_unauthorized()

    now = datetime.now(timezone.utc)
    identity = AdminIdentity(username=username, display_name=username)
    session_token = jwt.encode(
        {
            "sub": identity.username,
            "display_name": identity.display_name,
            "scope": "v6_admin",
            "iat": now,
            "exp": now + timedelta(seconds=_ADMIN_SESSION_TTL_SECONDS),
            "jti": secrets.token_urlsafe(16),
        },
        jwt_secret,
        algorithm="HS256",
    )
    _set_admin_cookies(
        response,
        session_token=session_token,
        csrf_token=secrets.token_urlsafe(32),
    )
    return {"user": {"username": identity.username, "display_name": identity.display_name}}


@router.get("/admin/auth/me")
def admin_me(identity: AdminIdentity = Depends(_require_admin)) -> dict[str, Any]:
    return {"user": {"username": identity.username, "display_name": identity.display_name}}


@router.post("/admin/auth/logout", status_code=204)
def admin_logout(
    response: Response,
    _identity: AdminIdentity = Depends(_require_admin),
    _csrf: None = Depends(_require_csrf),
) -> Response:
    _clear_admin_cookies(response)
    response.status_code = 204
    return response


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "v6.0"}


@router.get("/health/db")
def database_health(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1")).scalar_one()
        alembic_version = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if not alembic_version:
            raise RuntimeError("alembic version is unavailable")
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(
            status_code=503,
            detail={
                "code": "database_unavailable",
                "message": "Database connectivity or migration state is unavailable.",
            },
        ) from exc
    return {"status": "ok", "alembic_version": str(alembic_version)}


@router.post("/sessions", status_code=201)
def create_session(
    request: CreateSessionRequest, db: Session = Depends(get_db)
) -> Any:
    try:
        session, initial = sessions.create(db, request)
        return {"session": session_snapshot(session), "initial_turn": {
            "id": initial.id,
            "turn_index": initial.turn_index,
            "role": initial.role,
            "content": initial.content,
            "client_turn_id": initial.client_turn_id,
            "input_mode": initial.input_mode,
            "answer_duration_ms": initial.answer_duration_ms,
            "phase": initial.phase,
            "session_action": initial.session_action,
            "finish_reason": initial.finish_reason,
            "quality_flags": initial.quality_flags or [],
            "created_at": initial.created_at.isoformat() if initial.created_at else None,
        }}
    except ServiceError as exc:
        return _service_error(exc)


@router.get("/sessions/{session_uuid}")
def get_session(session_uuid: str, db: Session = Depends(get_db)) -> Any:
    try:
        return session_snapshot(sessions.get(db, session_uuid))
    except ServiceError as exc:
        return _service_error(exc)


TURN_STREAM_HEARTBEAT_SECONDS = 10.0
_turn_stream_tasks: set[asyncio.Task[None]] = set()


def _turn_stream_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ServiceError):
        return {
            "event": "error",
            "code": exc.code,
            "message": exc.message,
            "data": {"exception_type": type(exc).__name__},
        }
    model_empty_response = (
        isinstance(exc, ModelGatewayError)
        and exc.error_code == "model_empty_response"
    )
    model_connection_interrupted = (
        isinstance(exc, ModelGatewayError)
        and exc.transient
        and not model_empty_response
    )
    if model_empty_response:
        code = "model_empty_response"
        message = "模型暂时未返回有效内容；你的回答已保存，可以安全重试。"
    elif model_connection_interrupted:
        code = "model_connection_interrupted"
        message = "与访谈模型的连接暂时中断，已保存你的回答。请重试。"
    else:
        code = "turn_processing_failed"
        message = "本轮处理中断，已保留可恢复状态。"
    return {
        "event": "error",
        "code": code,
        "message": message,
        "data": {
            "exception_type": type(exc).__name__,
            "error_type": getattr(exc, "error_code", None),
            "attempt_count": getattr(exc, "attempt_count", 0),
            "latency_ms": getattr(exc, "latency_ms", 0),
        },
    }


def _run_turn_submission(
    session_factory: Callable[[], Session],
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[tuple[str, Any]],
    session_uuid: str,
    request: SubmitTurnRequest,
) -> None:
    user_turn_saved = False

    def publish(kind: str, payload: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))
        except RuntimeError:
            # The server may be shutting down after the browser disconnected.
            # Delivery can stop, but the worker must still finish persistence.
            pass

    def emit_event(event: dict[str, Any]) -> None:
        nonlocal user_turn_saved
        if event.get("event") == "user_turn_saved":
            user_turn_saved = True
        publish("event", event)

    try:
        with session_factory() as worker_db:
            sessions.submit(
                worker_db,
                session_uuid,
                request,
                emit=emit_event,
            )
    except ServiceError as exc:
        publish("service_error", exc)
    except Exception as exc:
        publish("exception", exc)
    finally:
        if user_turn_saved:
            try:
                # submit() holds the per-session lock until it returns or
                # unwinds. Schedule only afterwards, including the failure
                # path, so a saved answer is never skipped or deadlocked.
                sessions.schedule_evidence_snapshot(session_factory, session_uuid)
            except Exception as snapshot_exc:
                # Snapshot diagnostics are independent of the authoritative
                # interview stream. A scheduling failure must not replace a
                # persisted interviewer result or its original error.
                try:
                    with session_factory() as anomaly_db:
                        persisted_session = anomaly_db.scalar(
                            select(AssessmentSession).where(
                                AssessmentSession.uuid == session_uuid
                            )
                        )
                        if persisted_session is not None:
                            saved_turn_id = anomaly_db.scalar(
                                select(DialogueTurn.id).where(
                                    DialogueTurn.session_id
                                    == persisted_session.id,
                                    DialogueTurn.role == "user",
                                    DialogueTurn.client_turn_id
                                    == request.client_turn_id,
                                )
                            )
                            anomaly_db.add(
                                TechnicalAnomaly(
                                    session_id=persisted_session.id,
                                    turn_id=saved_turn_id,
                                    category="evidence_snapshot_schedule_failure",
                                    detail=(
                                        f"{type(snapshot_exc).__name__}: "
                                        f"{str(snapshot_exc)[:900]}"
                                    ),
                                    recoverable=True,
                                )
                            )
                            anomaly_db.commit()
                except Exception:
                    # Persistence can share the same infrastructure failure;
                    # either way the interview stream remains authoritative.
                    pass
        publish("done", None)


async def _turn_event_stream(
    first_item: tuple[str, Any],
    queue: asyncio.Queue[tuple[str, Any]],
) -> AsyncIterator[str]:
    item = first_item
    while True:
        kind, payload = item
        if kind == "done":
            return
        if kind == "event":
            yield json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        elif kind in {"service_error", "exception"}:
            yield json.dumps(
                _turn_stream_error(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            return

        try:
            item = await asyncio.wait_for(
                queue.get(), timeout=TURN_STREAM_HEARTBEAT_SECONDS
            )
        except asyncio.TimeoutError:
            item = ("event", {"event": "heartbeat"})


@router.post("/sessions/{session_uuid}/turns:stream")
async def submit_turn_stream(
    session_uuid: str,
    request: SubmitTurnRequest,
    worker_session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> Any:
    # The background model call owns its Session, so no SQLAlchemy state crosses
    # request/event-loop/thread boundaries.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        asyncio.to_thread(
            _run_turn_submission,
            worker_session_factory,
            loop,
            queue,
            session_uuid,
            request,
        )
    )
    # Keep the worker alive when the browser disconnects. The persisted
    # submission remains recoverable through its original client_turn_id.
    _turn_stream_tasks.add(task)
    task.add_done_callback(_turn_stream_tasks.discard)

    first_item = await queue.get()
    kind, payload = first_item
    if kind == "service_error":
        return _service_error(payload)
    status_code = 500 if kind == "exception" else 200
    return StreamingResponse(
        _turn_event_stream(first_item, queue),
        media_type="application/x-ndjson",
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_uuid}/finalize")
def finalize_session(
    session_uuid: str,
    request: FinalizeSessionRequest = Body(default_factory=FinalizeSessionRequest),
    db: Session = Depends(get_db),
) -> Any:
    try:
        session = sessions.finalize(db, session_uuid, request)
        return {"session": session_snapshot(session), "report": serialize_report(session)}
    except ServiceError as exc:
        return _service_error(exc)


@router.post(
    "/sessions/{session_uuid}/closure-suggestions/{closure_turn_id}/accept"
)
def accept_closure_suggestion(
    session_uuid: str,
    closure_turn_id: int,
    request: AcceptClosureSuggestionRequest,
    db: Session = Depends(get_db),
) -> Any:
    try:
        session = sessions.accept_closure_suggestion(
            db,
            session_uuid,
            closure_turn_id,
            request.expected_transcript_fingerprint,
        )
        return {"session": session_snapshot(session), "report": serialize_report(session)}
    except ServiceError as exc:
        return _service_error(exc)


@router.post("/sessions/{session_uuid}/report-readiness")
def report_readiness(
    session_uuid: str,
    retry_failed: bool = Query(default=False),
    db: Session = Depends(get_db),
    worker_session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> Any:
    """Check aggregate evidence readiness without freezing or scoring a session."""

    try:
        result = sessions.report_readiness(
            db,
            session_uuid,
            worker_session_factory,
            retry_failed=retry_failed,
        )
        return JSONResponse(
            status_code=202 if result["status"] == "checking" else 200,
            content=result,
        )
    except ServiceError as exc:
        return _service_error(exc)


@router.post(
    "/admin/sessions/{session_uuid}/finalize",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
def admin_finalize_session(session_uuid: str, db: Session = Depends(get_db)) -> Any:
    """Retry only the frozen, idempotent report finalization path."""

    try:
        session = sessions.finalize(db, session_uuid)
        return {"session": session_snapshot(session), "report": serialize_report(session)}
    except ServiceError as exc:
        return _service_error(exc)


@router.post("/sessions/{session_uuid}/exit")
def exit_session(
    session_uuid: str,
    request: ExitRequest = Body(default_factory=ExitRequest),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return session_snapshot(sessions.exit(db, session_uuid, request.reason))
    except ServiceError as exc:
        return _service_error(exc)


@router.get("/sessions/{session_uuid}/report")
def get_report(session_uuid: str, db: Session = Depends(get_db)) -> Any:
    try:
        session = sessions.get(db, session_uuid)
        report = serialize_report(session)
        if not report:
            raise ServiceError(409, "report_not_ready", "会话尚未完成，报告不可用。")
        return report
    except ServiceError as exc:
        return _service_error(exc)


def _report_pdf_bytes(report: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = "STSong-Light"
    doc = SimpleDocTemplate(output, pagesize=A4, title="思衡 V6 自然访谈报告")
    dimensions = list(report.get("dimensions", []))
    overall_score, measured_count = _public_overall_score(dimensions)
    total_note = (
        f"综合总分：{overall_score}（基于 {measured_count} 个证据充分维度计算）"
        if measured_count
        else "综合总分：—（暂无可计算维度）"
    )
    story = [
        Paragraph("思衡访谈报告", styles["Title"]),
        Spacer(1, 8),
        Paragraph(escape(str(report.get("summary", ""))), styles["BodyText"]),
        Paragraph(escape(total_note), styles["Heading2"]),
        Spacer(1, 10),
    ]
    if any(
        item.get("status") in {"limited", "unmeasured"}
        for item in dimensions
    ):
        story.extend(
            [
                Paragraph("关于“证据有限”", styles["Heading2"]),
                Paragraph(
                    escape(_PUBLIC_LIMITED_EVIDENCE_NOTICE),
                    styles["BodyText"],
                ),
                Spacer(1, 10),
            ]
        )
    for item in dimensions:
        score_text = _public_score_label(item["score"]) if item.get("score") is not None else (
            "证据有限" if item.get("status") == "limited" else "未充分测得"
        )
        story.extend(
            [
                Paragraph(
                    f"{escape(item['dimension_name'])}：{escape(score_text)}",
                    styles["Heading2"],
                ),
                Paragraph(escape(item.get("reason", "")), styles["BodyText"]),
                Paragraph("优势：" + escape(_public_dimension_strength(item)), styles["BodyText"]),
                Paragraph(
                    "建议：" + escape(_public_dimension_suggestion(item.get("dimension_key"))),
                    styles["BodyText"],
                ),
            ]
        )
        for evidence in item.get("evidences", [])[:3]:
            story.append(
                Paragraph(
                    escape(_public_evidence_source_label(evidence))
                    + "："
                    + escape(evidence.get("quote", "")),
                    styles["BodyText"],
                )
            )
        story.append(Spacer(1, 8))
    story.extend(
        [
            Spacer(1, 10),
            Paragraph(escape(report.get("disclaimer", "")), styles["BodyText"]),
        ]
    )
    doc.build(story)
    return output.getvalue()


def _public_evidence_source_label(evidence: dict[str, Any]) -> str:
    answer_ordinal = evidence.get("answer_ordinal")
    if isinstance(answer_ordinal, int) and answer_ordinal >= 1:
        return f"用户原话（第 {answer_ordinal} 次回答）"
    turn_index = evidence.get("turn_index")
    if isinstance(turn_index, int) and turn_index >= 0:
        return f"用户原话（对话记录 #{turn_index}）"
    return "用户原话"


def _public_score_label(score: int | float) -> str:
    """Render the fixed 1–5 evidence score as a participant-facing 100-point score."""

    return f"{round(float(score) * 20)} 分"


def _public_overall_score(dimensions: list[dict[str, Any]]) -> tuple[str, int]:
    scores = [
        float(item["score"])
        for item in dimensions
        if item.get("status") == "sufficient" and isinstance(item.get("score"), (int, float))
    ]
    if not scores:
        return "—", 0
    return _public_score_label(sum(scores) / len(scores)), len(scores)


def _public_dimension_strength(item: dict[str, Any]) -> str:
    if item.get("status") != "sufficient" or item.get("score") is None:
        return "本次证据有限，暂不形成该维度的优势判断。"
    return str(item.get("strength") or item.get("reason") or "本次原话支持了这一维度的观察。")


def _public_dimension_suggestion(dimension_key: object) -> str:
    return _PUBLIC_DIMENSION_SUGGESTIONS.get(
        str(dimension_key), "继续记录判断依据和可能改变想法的条件。"
    )


@router.get("/sessions/{session_uuid}/report.pdf")
def get_report_pdf(session_uuid: str, db: Session = Depends(get_db)) -> Any:
    report_response = get_report(session_uuid, db)
    if isinstance(report_response, JSONResponse):
        return report_response
    return Response(
        _report_pdf_bytes(report_response),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="assessment-v6-{session_uuid}.pdf"'
        },
    )


@router.get("/sessions/{session_uuid}/turns/{turn_index}/speech")
async def speech(
    session_uuid: str, turn_index: int, db: Session = Depends(get_db)
) -> Any:
    try:
        session = sessions.get(db, session_uuid)
        turn = db.scalar(
            select(DialogueTurn).where(
                DialogueTurn.session_id == session.id,
                DialogueTurn.turn_index == turn_index,
                DialogueTurn.role == "assistant",
            )
        )
        if not turn:
            raise ServiceError(
                404, "assistant_turn_not_found", "指定的已持久化 AI turn 不存在。"
            )
        result = await TTSService().synthesize(
            PersistedAITurn(
                id=turn.id,
                session_uuid=session.uuid,
                turn_index=turn.turn_index,
                role="assistant",
                content=turn.content,
                persisted=True,
            )
        )
        if result.ok:
            return Response(
                result.audio,
                media_type=result.content_type,
                headers={
                    "X-TTS-Provider": result.provider,
                    "X-TTS-Request-ID": result.request_id or "",
                    "Cache-Control": "no-store",
                },
            )
        db.add(
            TechnicalAnomaly(
                session_id=session.id,
                turn_id=turn.id,
                category="tts_fallback",
                detail=result.fallback_reason or "tts_unavailable",
                recoverable=True,
            )
        )
        db.commit()
        return JSONResponse(
            status_code=503,
            content={
                "code": "tts_fallback_required",
                "message": "服务端语音不可用，请使用浏览器语音回退。",
                "provider": result.provider,
                "fallback_reason": result.fallback_reason,
            },
            headers={"X-TTS-Fallback": "browser"},
        )
    except ServiceError as exc:
        return _service_error(exc)


def _count_rows(db: Session, statement: Any) -> int:
    return int(db.scalar(statement) or 0)


@router.get("/admin/dashboard/overview", dependencies=[Depends(_require_admin)])
def admin_dashboard_overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return only aggregate operational data needed by the review landing page.

    The dashboard intentionally omits dialogue text, evidence quotations, review
    notes, raw model payloads, and calibration confidence values.  Those remain
    available only in the authenticated, per-session review screen.
    """

    known_phases = (
        "interviewing",
        "finalizing",
        "completed",
        "exited",
        "safety_stopped",
    )
    phase_counts = {phase: 0 for phase in known_phases}
    for phase, count in db.execute(
        select(AssessmentSession.phase, func.count(AssessmentSession.id)).group_by(
            AssessmentSession.phase
        )
    ):
        phase_counts[str(phase)] = int(count)

    review_counts = {
        "pending": 0,
        "in_review": 0,
        "approved": 0,
        "needs_followup": 0,
    }
    review_status = func.coalesce(HumanReview.status, "pending")
    for status, count in db.execute(
        select(review_status, func.count(AssessmentSession.id))
        .select_from(AssessmentSession)
        .outerjoin(HumanReview)
        .group_by(review_status)
    ):
        review_counts[str(status)] = int(count)

    total = sum(phase_counts.values())
    recent_rows = db.execute(
        select(AssessmentSession, HumanReview.status)
        .outerjoin(HumanReview)
        .order_by(AssessmentSession.updated_at.desc())
        .limit(8)
    ).all()
    return {
        "measurement": {
            "total_sessions": total,
            "completed_sessions": phase_counts["completed"],
            "active_sessions": phase_counts["interviewing"] + phase_counts["finalizing"],
            "completion_rate": round(phase_counts["completed"] / total, 4) if total else 0,
            "phase_counts": phase_counts,
        },
        "review_queue": {
            **review_counts,
            "manual_review_recommended": _count_rows(
                db,
                select(func.count(AssessmentSession.id)).where(
                    AssessmentSession.manual_review_recommended.is_(True)
                ),
            ),
            "expert_scored_sessions": _count_rows(
                db, select(func.count(func.distinct(ExpertScore.session_id)))
            ),
        },
        "pipeline_health": {
            "reports_generated": _count_rows(db, select(func.count(AssessmentReport.id))),
            "scoring_failures": _count_rows(
                db,
                select(func.count(ScoringRun.id)).where(ScoringRun.status == "failed"),
            ),
            "failed_traces": _count_rows(
                db,
                select(func.count(AgentTrace.id)).where(
                    AgentTrace.renderer_status == "failed"
                ),
            ),
            "repaired_traces": _count_rows(
                db,
                select(func.count(AgentTrace.id)).where(AgentTrace.repair_used.is_(True)),
            ),
            "technical_anomalies": _count_rows(
                db, select(func.count(TechnicalAnomaly.id))
            ),
        },
        "recent_sessions": [
            {
                "uuid": session.uuid,
                "display_name": session.display_name,
                "phase": session.phase,
                "user_answer_count": session.user_answer_count,
                "manual_review_recommended": session.manual_review_recommended,
                "review_status": status or "pending",
                "updated_at": session.updated_at.isoformat(),
            }
            for session, status in recent_rows
        ],
    }


@router.get("/admin/sessions", dependencies=[Depends(_require_admin)])
def admin_sessions(
    phase: Optional[str] = None,
    review_status: Optional[str] = None,
    manual_review_recommended: Optional[bool] = None,
    low_confidence: Optional[bool] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(AssessmentSession).outerjoin(HumanReview)
    if phase:
        stmt = stmt.where(AssessmentSession.phase == phase)
    if review_status:
        # Historical rows created before review records existed are still
        # actionable pending sessions, not invisible data.
        stmt = stmt.where(func.coalesce(HumanReview.status, "pending") == review_status)
    if manual_review_recommended is not None:
        stmt = stmt.where(
            AssessmentSession.manual_review_recommended == manual_review_recommended
        )
    if low_confidence is True:
        stmt = stmt.where(AssessmentSession.manual_review_recommended.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                AssessmentSession.uuid.like(like),
                AssessmentSession.display_name.like(like),
            )
        )
    rows = list(db.scalars(stmt.order_by(AssessmentSession.created_at.desc())))
    return {
        "items": [
            {
                "uuid": item.uuid,
                "phase": item.phase,
                "display_name": item.display_name,
                "occupation": item.occupation,
                "user_answer_count": item.user_answer_count,
                "manual_review_recommended": item.manual_review_recommended,
                "review_status": item.review.status if item.review else "pending",
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in rows
        ],
        "total": len(rows),
    }


@router.get("/admin/sessions/{session_uuid}", dependencies=[Depends(_require_admin)])
def admin_session_detail(
    session_uuid: str, db: Session = Depends(get_db)
) -> Any:
    try:
        session = sessions.get(db, session_uuid)
        snapshot = session_snapshot(session)
        turn_index = {turn.id: turn.turn_index for turn in session.turns}
        ordered_turns = sorted(session.turns, key=lambda item: item.turn_index)
        preceding_question_by_user_index: dict[int, str | None] = {}
        latest_assistant: str | None = None
        for turn in ordered_turns:
            if turn.role == "assistant":
                latest_assistant = turn.content
            elif turn.role == "user":
                preceding_question_by_user_index[turn.turn_index] = latest_assistant
        final_dimensions_by_span: dict[int, set[str]] = {}
        for evidence in session.evidence_items:
            if evidence.attribution_span_id is not None:
                final_dimensions_by_span.setdefault(
                    evidence.attribution_span_id, set()
                ).add(evidence.dimension_key)
        snapshot_dimensions_by_span: dict[int, set[str]] = {}
        latest_attribution_check = max(
            session.readiness_checks,
            key=lambda item: item.id,
            default=None,
        )
        latest_attribution_check_id = (
            latest_attribution_check.id
            if latest_attribution_check is not None
            else None
        )
        latest_attribution_spans = [
            span
            for span in session.evidence_attribution_spans
            if latest_attribution_check_id is not None
            and span.readiness_check_id == latest_attribution_check_id
        ]
        if (
            latest_attribution_check is not None
            and isinstance(latest_attribution_check.result_data, dict)
        ):
            for dimension in latest_attribution_check.result_data.get(
                "dimensions", []
            ):
                if not isinstance(dimension, dict):
                    continue
                dimension_key = dimension.get("dimension_key")
                for reference in dimension.get("evidence_refs", []):
                    if (
                        isinstance(reference, dict)
                        and isinstance(reference.get("attribution_span_id"), int)
                        and isinstance(dimension_key, str)
                    ):
                        snapshot_dimensions_by_span.setdefault(
                            reference["attribution_span_id"], set()
                        ).add(dimension_key)
        snapshot.update(
            {
                "review_status": session.review.status if session.review else "pending",
                "review_notes": session.review.notes if session.review else None,
                "review_decision": session.review.decision if session.review else None,
                "reviewer": session.review.reviewer if session.review else None,
                "reviewed_at": (
                    session.review.reviewed_at.isoformat()
                    if session.review and session.review.reviewed_at
                    else None
                ),
                "evidence_items": [
                    {
                        "dimension_key": evidence.dimension_key,
                        "turn_index": turn_index.get(evidence.user_turn_id),
                        "quote": evidence.quote,
                        "quote_start": evidence.quote_start,
                        "quote_end": evidence.quote_end,
                        "confidence": evidence.confidence,
                        "attribution_span_id": evidence.attribution_span_id,
                        "readiness_check_id": evidence.readiness_check_id,
                        "validation_status": evidence.validation_status,
                        "validation_reason": evidence.validation_reason,
                        "source_type": "user",
                        "status": "sufficient",
                        "active_for_scoring": True,
                    }
                    for evidence in sorted(session.evidence_items, key=lambda item: item.id)
                ],
                "evidence_attributions": [
                    {
                        "id": span.id,
                        "span_id": span.id,
                        "readiness_check_id": span.readiness_check_id,
                        "turn_index": span.turn_index,
                        "quote": span.quote,
                        "start": span.start,
                        "end": span.end,
                        "text_hash": span.text_hash,
                        "owner": span.owner,
                        "relation": span.relation,
                        "source_label": span.source_label,
                        "elicitation_level": span.elicitation_level,
                        "confidence": span.confidence,
                        "reason": span.reason,
                        "eligibility": span.eligibility,
                        "validation_status": span.validation_status,
                        "validation_reason": span.validation_reason,
                        "eliciting_question": preceding_question_by_user_index.get(
                            span.turn_index
                        ),
                        "used_dimension_keys": sorted(
                            final_dimensions_by_span.get(span.id, set())
                        ),
                        "final_scoring_dimension_keys": sorted(
                            final_dimensions_by_span.get(span.id, set())
                        ),
                        "snapshot_used_dimension_keys": sorted(
                            snapshot_dimensions_by_span.get(span.id, set())
                        ),
                        "transcript_fingerprint": span.transcript_fingerprint,
                        "asset_fingerprint": span.asset_fingerprint,
                        "prompt_template_id": span.prompt_template_id,
                        "prompt_version": span.prompt_version,
                        "schema_version": span.schema_version,
                        "created_at": span.created_at.isoformat(),
                    }
                    for span in sorted(
                        latest_attribution_spans,
                        key=lambda item: (item.turn_index, item.start, item.id),
                    )
                ],
                "scoring_runs": [
                    {
                        "id": run.id,
                        "attempt_number": run.attempt_number,
                        "status": run.status,
                        "transcript_fingerprint": run.transcript_fingerprint,
                        "model": f"{run.model_provider}/{run.model_name}",
                        "prompt_template_id": run.prompt_template_id,
                        "prompt_version": run.prompt_version,
                        "repair_used": run.repair_used,
                        "error": run.error,
                        "manual_review_recommended": run.manual_review_recommended,
                        "result_data": run.result_data,
                        "created_at": run.created_at.isoformat(),
                        "completed_at": (
                            run.completed_at.isoformat() if run.completed_at else None
                        ),
                    }
                    for run in sorted(session.scoring_runs, key=lambda item: item.attempt_number)
                ],
                "traces": [
                    {
                        "id": trace.id,
                        "turn_index": turn_index.get(trace.assistant_turn_id),
                        "action": trace.action,
                        "model": f"{trace.model_provider}/{trace.model_name}",
                        "prompt_template_id": trace.prompt_template_id,
                        "prompt_version": trace.prompt_version,
                        "input_fingerprint": trace.input_fingerprint,
                        "output_contract": trace.output_contract,
                        "renderer_status": trace.renderer_status,
                        "repair_used": trace.repair_used,
                        "fallback_used": trace.fallback_used,
                        "fallback_reason": trace.fallback_reason,
                        "latency_ms": trace.latency_ms,
                        "created_at": trace.created_at.isoformat(),
                    }
                    for trace in sorted(session.traces, key=lambda item: item.id)
                ],
                "technical_anomalies": [
                    {
                        "category": anomaly.category,
                        "detail": anomaly.detail,
                        "recoverable": anomaly.recoverable,
                        "turn_index": turn_index.get(anomaly.turn_id),
                    }
                    for anomaly in sorted(session.anomalies, key=lambda item: item.id)
                ],
                "expert_scores": [
                    {
                        "dimension_key": score.dimension_key,
                        "score": score.score,
                        "comment": score.comment or "",
                        "reviewer": score.reviewer,
                    }
                    for score in session.expert_scores
                ],
                "report": serialize_report(session),
            }
        )
        return snapshot
    except ServiceError as exc:
        return _service_error(exc)


def _update_review(
    session_uuid: str, request: ReviewRequest, db: Session, *, reviewer: str
) -> Any:
    try:
        session = sessions.get(db, session_uuid)
        review = session.review
        if not review:
            review = HumanReview(session_id=session.id)
            db.add(review)
        review.status = request.status
        review.decision = request.decision
        review.notes = request.notes
        review.reviewer = reviewer
        review.reviewed_at = utcnow()
        db.commit()
        return {
            "review_status": review.status,
            "review_notes": review.notes,
            "decision": review.decision,
            "reviewer": review.reviewer,
            "reviewed_at": review.reviewed_at.isoformat(),
        }
    except ServiceError as exc:
        return _service_error(exc)


@router.put(
    "/admin/sessions/{session_uuid}/review",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
def put_review(
    session_uuid: str,
    request: ReviewRequest,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(_require_admin),
) -> Any:
    return _update_review(
        session_uuid,
        request,
        db,
        reviewer=_effective_reviewer(request.reviewer, identity),
    )


@router.patch(
    "/admin/sessions/{session_uuid}/review",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
def patch_review(
    session_uuid: str,
    request: ReviewRequest,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(_require_admin),
) -> Any:
    return _update_review(
        session_uuid,
        request,
        db,
        reviewer=_effective_reviewer(request.reviewer, identity),
    )


def _save_expert_scores(
    session_uuid: str, request: ExpertScoresRequest, db: Session, *, reviewer: str
) -> Any:
    try:
        session = sessions.get(db, session_uuid)
        saved = []
        for item in request.scores:
            if item.dimension_key not in DIMENSION_BY_KEY:
                raise ServiceError(
                    422, "unknown_dimension", f"未知维度：{item.dimension_key}"
                )
            if item.score is None:
                continue
            record = db.scalar(
                select(ExpertScore).where(
                    ExpertScore.session_id == session.id,
                    ExpertScore.dimension_key == item.dimension_key,
                    ExpertScore.reviewer == reviewer,
                )
            )
            if not record:
                record = ExpertScore(
                    session_id=session.id,
                    dimension_key=item.dimension_key,
                    reviewer=reviewer,
                    score=item.score,
                )
                db.add(record)
            record.score = item.score
            record.comment = item.comment
            saved.append(
                {
                    "dimension_key": item.dimension_key,
                    "score": item.score,
                    "comment": item.comment or "",
                }
            )
        db.commit()
        return {"saved": saved, "reviewer": reviewer}
    except ServiceError as exc:
        return _service_error(exc)


@router.put(
    "/admin/sessions/{session_uuid}/expert-scores",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
def put_expert_scores(
    session_uuid: str,
    request: ExpertScoresRequest,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(_require_admin),
) -> Any:
    return _save_expert_scores(
        session_uuid,
        request,
        db,
        reviewer=_effective_reviewer(request.reviewer, identity),
    )


@router.post(
    "/admin/sessions/{session_uuid}/expert-scores",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
def post_expert_scores(
    session_uuid: str,
    request: ExpertScoresRequest,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(_require_admin),
) -> Any:
    return _save_expert_scores(
        session_uuid,
        request,
        db,
        reviewer=_effective_reviewer(request.reviewer, identity),
    )


@router.post(
    "/admin/expert-scores:import",
    dependencies=[Depends(_require_admin), Depends(_require_csrf)],
)
async def import_expert_scores(
    file: UploadFile = File(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    raw = await file.read()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {
            "imported": 0,
            "errors": [{"row": 0, "reason": "CSV 必须为 UTF-8 编码"}],
        }
    imported = 0
    errors: list[dict[str, Any]] = []
    for row_number, row in enumerate(csv.DictReader(io.StringIO(decoded)), start=2):
        try:
            session = db.scalar(
                select(AssessmentSession).where(
                    AssessmentSession.uuid == row.get("session_uuid", "")
                )
            )
            dimension_key = row.get("dimension_key", "")
            score = int(row.get("score", ""))
            reviewer = (row.get("reviewer") or "expert").strip()
            if not session:
                raise ValueError("session_not_found")
            if dimension_key not in DIMENSION_BY_KEY:
                raise ValueError("unknown_dimension")
            if score < 1 or score > 5:
                raise ValueError("score_out_of_range")
            record = db.scalar(
                select(ExpertScore).where(
                    ExpertScore.session_id == session.id,
                    ExpertScore.dimension_key == dimension_key,
                    ExpertScore.reviewer == reviewer,
                )
            )
            if not record:
                record = ExpertScore(
                    session_id=session.id,
                    dimension_key=dimension_key,
                    reviewer=reviewer,
                    score=score,
                )
                db.add(record)
            record.score = score
            record.comment = row.get("comment") or None
            imported += 1
        except Exception as exc:
            errors.append({"row": row_number, "reason": str(exc)})
    db.commit()
    return {"imported": imported, "errors": errors}


def _anonymous_zip(db: Session) -> bytes:
    all_sessions = list(
        db.scalars(select(AssessmentSession).order_by(AssessmentSession.id))
    )
    anonymous_ids: dict[int, str] = {}
    used_ids: set[str] = set()
    for session in all_sessions:
        anonymous_id = secrets.token_hex(8)
        while anonymous_id in used_ids:
            anonymous_id = secrets.token_hex(8)
        used_ids.add(anonymous_id)
        anonymous_ids[session.id] = anonymous_id

    sessions_json: list[dict[str, Any]] = []
    reports_json: list[dict[str, Any]] = []
    turns_io = io.StringIO()
    evidence_io = io.StringIO()
    turns_writer = csv.writer(turns_io)
    evidence_writer = csv.writer(evidence_io)
    turns_writer.writerow(
        [
            "anonymous_session_id",
            "turn_index",
            "role",
            "phase",
            "input_mode",
            "answer_duration_ms",
        ]
    )
    evidence_writer.writerow(
        [
            "anonymous_session_id",
            "turn_index",
            "dimension_key",
            "quote_length",
            "confidence",
        ]
    )
    for session in all_sessions:
        anonymous_id = anonymous_ids[session.id]
        sessions_json.append(
            {
                "anonymous_session_id": anonymous_id,
                "phase": session.phase,
                "user_answer_count": session.user_answer_count,
                "manual_review_recommended": session.manual_review_recommended,
            }
        )
        turn_indexes = {turn.id: turn.turn_index for turn in session.turns}
        for turn in sorted(session.turns, key=lambda item: item.turn_index):
            turns_writer.writerow(
                [
                    anonymous_id,
                    turn.turn_index,
                    turn.role,
                    turn.phase,
                    turn.input_mode,
                    turn.answer_duration_ms,
                ]
            )
        for evidence in session.evidence_items:
            evidence_writer.writerow(
                [
                    anonymous_id,
                    turn_indexes.get(evidence.user_turn_id),
                    evidence.dimension_key,
                    len(evidence.quote),
                    evidence.confidence,
                ]
            )
        if session.report:
            source = session.report.report_data
            reports_json.append(
                {
                    "anonymous_session_id": anonymous_id,
                    "experimental_notice": source.get("experimental_notice"),
                    "dimensions": [
                        {
                            "dimension_key": dimension.get("dimension_key"),
                            "dimension_name": dimension.get("dimension_name"),
                            "status": dimension.get("status"),
                            "score": dimension.get("score"),
                            "suggestion": dimension.get("suggestion"),
                            "observable_behaviors": dimension.get(
                                "observable_behaviors", []
                            ),
                        }
                        for dimension in source.get("dimensions", [])
                    ],
                    "manual_review_recommended": source.get(
                        "manual_review_recommended", False
                    ),
                    "disclaimer": source.get("disclaimer"),
                }
            )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "sessions.json", json.dumps(sessions_json, ensure_ascii=False, indent=2)
        )
        archive.writestr("turns.csv", turns_io.getvalue().encode("utf-8-sig"))
        archive.writestr("evidence.csv", evidence_io.getvalue().encode("utf-8-sig"))
        archive.writestr(
            "reports.json", json.dumps(reports_json, ensure_ascii=False, indent=2)
        )
    return output.getvalue()


@router.get("/admin/exports/anonymous", dependencies=[Depends(_require_admin)])
@router.get(
    "/admin/exports/anonymous.zip",
    dependencies=[Depends(_require_admin)],
    include_in_schema=False,
)
def anonymous_export(db: Session = Depends(get_db)) -> Response:
    return Response(
        _anonymous_zip(db),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="assessment-v6-anonymous.zip"'
        },
    )
