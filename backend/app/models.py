"""V6 persistence model.

The V6 schema deliberately records an interview as a plain transcript.  It
does not contain a stage graph, a question bank, a target dimension, or a
coverage controller: those concepts would make it too easy for runtime code to
silently turn the natural interview back into a scripted assessment.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AssessmentSession(Base):
    __tablename__ = "assessment_sessions"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('interviewing','finalizing','completed','exited','safety_stopped')",
            name="ck_session_phase",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid_lib.uuid4())
    )
    phase: Mapped[str] = mapped_column(String(24), default="interviewing", index=True)
    consent_version: Mapped[str] = mapped_column(String(40))
    consent_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    display_name: Mapped[Optional[str]] = mapped_column(String(100))
    occupation: Mapped[Optional[str]] = mapped_column(String(200))
    experience_level: Mapped[Optional[str]] = mapped_column(String(100))
    collaboration_role: Mapped[Optional[str]] = mapped_column(String(200))
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    user_answer_count: Mapped[int] = mapped_column(Integer, default=0)
    finalization_state: Mapped[str] = mapped_column(String(24), default="not_started")
    transcript_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    transcript_frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_early: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_review_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    turns: Mapped[list[DialogueTurn]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="DialogueTurn.turn_index"
    )
    submissions: Mapped[list[TurnSubmission]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    traces: Mapped[list[AgentTrace]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    anomalies: Mapped[list[TechnicalAnomaly]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    report: Mapped[Optional[AssessmentReport]] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
    review: Mapped[Optional[HumanReview]] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
    expert_scores: Mapped[list[ExpertScore]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    scoring_runs: Mapped[list[ScoringRun]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    evidence_items: Mapped[list[EvidenceItem]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    readiness_checks: Mapped[list[EvidenceReadinessCheck]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    evidence_attribution_spans: Mapped[list[EvidenceAttributionSpan]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class DialogueTurn(Base):
    __tablename__ = "dialogue_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_index", name="uq_turn_session_index"),
        UniqueConstraint("session_id", "client_turn_id", name="uq_turn_session_client_id"),
        CheckConstraint("role IN ('user','assistant')", name="ck_turn_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    turn_index: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(24), default="interviewing")
    content: Mapped[str] = mapped_column(Text)
    client_turn_id: Mapped[Optional[str]] = mapped_column(String(80))
    input_mode: Mapped[Optional[str]] = mapped_column(String(24))
    answer_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    session_action: Mapped[Optional[str]] = mapped_column(String(16))
    finish_reason: Mapped[Optional[str]] = mapped_column(String(40))
    quality_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(back_populates="turns")
    evidence_attribution_spans: Mapped[list[EvidenceAttributionSpan]] = relationship(
        back_populates="user_turn"
    )


class TurnSubmission(Base):
    __tablename__ = "turn_submissions"
    __table_args__ = (UniqueConstraint("session_id", "client_turn_id", name="uq_submission_client_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    client_turn_id: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    user_turn_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="SET NULL")
    )
    assistant_turn_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="SET NULL")
    )
    response_events: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)
    processing_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    session: Mapped[AssessmentSession] = relationship(back_populates="submissions")


class AgentTrace(Base):
    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    assistant_turn_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(80))
    model_provider: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_template_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    input_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    output_contract: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    renderer_status: Mapped[str] = mapped_column(String(40), default="accepted")
    repair_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_reason: Mapped[Optional[str]] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(back_populates="traces")


class TechnicalAnomaly(Base):
    __tablename__ = "technical_anomalies"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text)
    recoverable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(back_populates="anomalies")


class ScoringRun(Base):
    __tablename__ = "scoring_runs"
    __table_args__ = (UniqueConstraint("session_id", "attempt_number", name="uq_scoring_run_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    transcript_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    model_provider: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_template_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    repair_used: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    result_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    manual_review_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    session: Mapped[AssessmentSession] = relationship(back_populates="scoring_runs")


class EvidenceAttributionSpan(Base):
    """Versioned attribution for one exact span of participant input.

    ``eligibility`` and both validation fields are server decisions.  They are
    not copied from the attribution model's output contract.
    """

    __tablename__ = "evidence_attribution_spans"
    __table_args__ = (
        UniqueConstraint(
            "readiness_check_id",
            "user_turn_id",
            "span_start",
            "span_end",
            name="uq_attribution_check_turn_span",
        ),
        CheckConstraint("turn_index >= 0", name="ck_attribution_turn_index"),
        CheckConstraint("span_start >= 0", name="ck_attribution_start"),
        CheckConstraint("span_end > span_start", name="ck_attribution_end"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_attribution_confidence",
        ),
        CheckConstraint(
            "owner IN ('participant_owned','external_quoted','external_paraphrased','uncertain')",
            name="ck_attribution_owner",
        ),
        CheckConstraint(
            "relation IN ('own_reasoning','endorses','critiques','rejects','quotes_only','asks_or_requests')",
            name="ck_attribution_relation",
        ),
        CheckConstraint(
            "elicitation_level IN ('spontaneous','open_probe','focused_probe','strong_scaffold')",
            name="ck_attribution_elicitation",
        ),
        CheckConstraint(
            "eligibility IN ('eligible','context_only','manual_review')",
            name="ck_attribution_eligibility",
        ),
        CheckConstraint(
            "validation_status IN ('pending','validated','rejected','manual_review','legacy_unclassified')",
            name="ck_attribution_validation_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    user_turn_id: Mapped[int] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="CASCADE"), index=True
    )
    readiness_check_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evidence_readiness_checks.id", ondelete="CASCADE"), index=True
    )
    turn_index: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    start: Mapped[int] = mapped_column("span_start", Integer)
    end: Mapped[int] = mapped_column("span_end", Integer)
    text_hash: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(32))
    relation: Mapped[str] = mapped_column(String(32))
    elicitation_level: Mapped[str] = mapped_column(String(32))
    source_label: Mapped[Optional[str]] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    eligibility: Mapped[str] = mapped_column(String(24))
    validation_status: Mapped[str] = mapped_column(String(32), default="pending")
    validation_reason: Mapped[Optional[str]] = mapped_column(Text)
    transcript_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    asset_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    prompt_template_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(
        back_populates="evidence_attribution_spans"
    )
    user_turn: Mapped[DialogueTurn] = relationship(
        back_populates="evidence_attribution_spans"
    )
    readiness_check: Mapped[Optional[EvidenceReadinessCheck]] = relationship(
        back_populates="attribution_spans"
    )
    evidence_items: Mapped[list[EvidenceItem]] = relationship(
        back_populates="attribution_span"
    )


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        Index("ix_evidence_session_dimension", "session_id", "dimension_key"),
        CheckConstraint(
            "validation_status IN ('pending','validated','rejected','manual_review','legacy_unclassified')",
            name="ck_evidence_validation_status",
        ),
        CheckConstraint(
            "validation_status != 'validated' OR attribution_span_id IS NOT NULL",
            name="ck_evidence_validated_attribution",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    scoring_run_id: Mapped[int] = mapped_column(
        ForeignKey("scoring_runs.id", ondelete="CASCADE"), index=True
    )
    user_turn_id: Mapped[int] = mapped_column(
        ForeignKey("dialogue_turns.id", ondelete="CASCADE"), index=True
    )
    attribution_span_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evidence_attribution_spans.id", ondelete="RESTRICT"), index=True
    )
    readiness_check_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("evidence_readiness_checks.id", ondelete="SET NULL"), index=True
    )
    dimension_key: Mapped[str] = mapped_column(String(80))
    quote: Mapped[str] = mapped_column(Text)
    quote_start: Mapped[int] = mapped_column(Integer)
    quote_end: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    validation_status: Mapped[str] = mapped_column(
        String(32),
        default="legacy_unclassified",
        server_default="legacy_unclassified",
    )
    validation_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(back_populates="evidence_items")
    attribution_span: Mapped[Optional[EvidenceAttributionSpan]] = relationship(
        back_populates="evidence_items"
    )


class EvidenceReadinessCheck(Base):
    """Validated incremental evidence snapshot for one exact transcript.

    The snapshot remains private while the interview is open. Finalization may
    promote this exact result into the formal scoring/report records only after
    its transcript fingerprint has been frozen, so no second model score or
    stale-last-turn shortcut is needed.
    """

    __tablename__ = "evidence_readiness_checks"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "transcript_fingerprint",
            "asset_fingerprint",
            name="uq_readiness_session_transcript_asset",
        ),
        CheckConstraint(
            "status IN ('processing','ready','insufficient','failed')",
            name="ck_readiness_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    transcript_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    asset_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    ready: Mapped[Optional[bool]] = mapped_column(Boolean)
    sufficient_dimension_count: Mapped[Optional[int]] = mapped_column(Integer)
    model_provider: Mapped[str] = mapped_column(String(80), default="pending")
    model_name: Mapped[str] = mapped_column(String(120), default="pending")
    prompt_template_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    repair_used: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_user_turn_index: Mapped[Optional[int]] = mapped_column(Integer)
    result_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    session: Mapped[AssessmentSession] = relationship(back_populates="readiness_checks")
    attribution_spans: Mapped[list[EvidenceAttributionSpan]] = relationship(
        back_populates="readiness_check"
    )


class AssessmentReport(Base):
    __tablename__ = "assessment_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), unique=True
    )
    version: Mapped[str] = mapped_column(String(40), default="v6.0")
    report_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AssessmentSession] = relationship(back_populates="report")


class HumanReview(Base):
    __tablename__ = "human_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    decision: Mapped[Optional[str]] = mapped_column(String(80))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    reviewer: Mapped[Optional[str]] = mapped_column(String(120))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    session: Mapped[AssessmentSession] = relationship(back_populates="review")


class ExpertScore(Base):
    __tablename__ = "expert_scores"
    __table_args__ = (
        UniqueConstraint("session_id", "dimension_key", "reviewer", name="uq_expert_session_dimension_reviewer"),
        CheckConstraint("score >= 1 AND score <= 5", name="ck_expert_score_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    dimension_key: Mapped[str] = mapped_column(String(80))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(120), default="expert")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    session: Mapped[AssessmentSession] = relationship(back_populates="expert_scores")
