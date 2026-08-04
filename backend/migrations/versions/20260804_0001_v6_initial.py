"""V6 standalone natural-interview initial schema.

This migration intentionally starts a new isolated database. It carries forward
audit, review, expert-score, PDF, and anonymous-export support, but not the
former stage, coverage, issue-frame, or question-bank controller schema.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assessment_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("consent_version", sa.String(length=40), nullable=False),
        sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("occupation", sa.String(length=200), nullable=True),
        sa.Column("experience_level", sa.String(length=100), nullable=True),
        sa.Column("collaboration_role", sa.String(length=200), nullable=True),
        sa.Column("profile_data", sa.JSON(), nullable=False),
        sa.Column("user_answer_count", sa.Integer(), nullable=False),
        sa.Column("finalization_state", sa.String(length=24), nullable=False),
        sa.Column("transcript_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("transcript_frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_early", sa.Boolean(), nullable=False),
        sa.Column("manual_review_recommended", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "phase IN ('interviewing','finalizing','completed','exited','safety_stopped')",
            name="ck_session_phase",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assessment_sessions_uuid", "assessment_sessions", ["uuid"], unique=True)
    op.create_index("ix_assessment_sessions_phase", "assessment_sessions", ["phase"], unique=False)
    op.create_index(
        "ix_assessment_sessions_transcript_fingerprint",
        "assessment_sessions",
        ["transcript_fingerprint"],
        unique=False,
    )

    op.create_table(
        "dialogue_turns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_turn_id", sa.String(length=80), nullable=True),
        sa.Column("input_mode", sa.String(length=24), nullable=True),
        sa.Column("answer_duration_ms", sa.Integer(), nullable=True),
        sa.Column("session_action", sa.String(length=16), nullable=True),
        sa.Column("finish_reason", sa.String(length=40), nullable=True),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_turn_role"),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_turn_id", name="uq_turn_session_client_id"),
        sa.UniqueConstraint("session_id", "turn_index", name="uq_turn_session_index"),
    )
    op.create_index("ix_dialogue_turns_session_id", "dialogue_turns", ["session_id"], unique=False)

    op.create_table(
        "turn_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("client_turn_id", sa.String(length=80), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("user_turn_id", sa.Integer(), nullable=True),
        sa.Column("assistant_turn_id", sa.Integer(), nullable=True),
        sa.Column("response_events", sa.JSON(), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assistant_turn_id"], ["dialogue_turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_turn_id"], ["dialogue_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_turn_id", name="uq_submission_client_id"),
    )
    op.create_index("ix_turn_submissions_session_id", "turn_submissions", ["session_id"], unique=False)
    op.create_index("ix_turn_submissions_state", "turn_submissions", ["state"], unique=False)

    op.create_table(
        "agent_traces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("assistant_turn_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("model_provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("output_contract", sa.JSON(), nullable=False),
        sa.Column("renderer_status", sa.String(length=40), nullable=False),
        sa.Column("repair_used", sa.Boolean(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assistant_turn_id"], ["dialogue_turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_traces_session_id", "agent_traces", ["session_id"], unique=False)

    op.create_table(
        "technical_anomalies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("recoverable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["dialogue_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_technical_anomalies_session_id", "technical_anomalies", ["session_id"], unique=False
    )

    op.create_table(
        "scoring_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("transcript_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model_provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("repair_used", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_data", sa.JSON(), nullable=True),
        sa.Column("manual_review_recommended", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "attempt_number", name="uq_scoring_run_attempt"),
    )
    op.create_index("ix_scoring_runs_session_id", "scoring_runs", ["session_id"], unique=False)
    op.create_index("ix_scoring_runs_status", "scoring_runs", ["status"], unique=False)
    op.create_index(
        "ix_scoring_runs_transcript_fingerprint",
        "scoring_runs",
        ["transcript_fingerprint"],
        unique=False,
    )

    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("scoring_run_id", sa.Integer(), nullable=False),
        sa.Column("user_turn_id", sa.Integer(), nullable=False),
        sa.Column("dimension_key", sa.String(length=80), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scoring_run_id"], ["scoring_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_turn_id"], ["dialogue_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_items_session_id", "evidence_items", ["session_id"], unique=False)
    op.create_index("ix_evidence_items_scoring_run_id", "evidence_items", ["scoring_run_id"], unique=False)
    op.create_index("ix_evidence_items_user_turn_id", "evidence_items", ["user_turn_id"], unique=False)
    op.create_index(
        "ix_evidence_session_dimension",
        "evidence_items",
        ["session_id", "dimension_key"],
        unique=False,
    )

    op.create_table(
        "assessment_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("report_data", sa.JSON(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )

    op.create_table(
        "human_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("decision", sa.String(length=80), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index("ix_human_reviews_status", "human_reviews", ["status"], unique=False)

    op.create_table(
        "expert_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("dimension_key", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_expert_score_range"),
        sa.ForeignKeyConstraint(["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "dimension_key",
            "reviewer",
            name="uq_expert_session_dimension_reviewer",
        ),
    )
    op.create_index("ix_expert_scores_session_id", "expert_scores", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_table("expert_scores")
    op.drop_table("human_reviews")
    op.drop_table("assessment_reports")
    op.drop_table("evidence_items")
    op.drop_table("scoring_runs")
    op.drop_table("technical_anomalies")
    op.drop_table("agent_traces")
    op.drop_table("turn_submissions")
    op.drop_table("dialogue_turns")
    op.drop_table("assessment_sessions")
