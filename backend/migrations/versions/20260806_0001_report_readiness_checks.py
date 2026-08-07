"""Add the idempotent report-readiness preflight cache.

The table stores only aggregate readiness and model provenance.  It is kept
separate from formal scoring runs, evidence items, and reports so checking an
open transcript cannot be mistaken for a completed assessment.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260806_0001"
down_revision = "20260804_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_readiness_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("transcript_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("asset_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=True),
        sa.Column("sufficient_dimension_count", sa.Integer(), nullable=True),
        sa.Column("model_provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("repair_used", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing','ready','insufficient','failed')",
            name="ck_readiness_status",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["assessment_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "transcript_fingerprint",
            "asset_fingerprint",
            name="uq_readiness_session_transcript_asset",
        ),
    )
    op.create_index(
        "ix_evidence_readiness_checks_session_id",
        "evidence_readiness_checks",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_readiness_checks_transcript_fingerprint",
        "evidence_readiness_checks",
        ["transcript_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_readiness_checks_status",
        "evidence_readiness_checks",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_readiness_checks_status",
        table_name="evidence_readiness_checks",
    )
    op.drop_index(
        "ix_evidence_readiness_checks_transcript_fingerprint",
        table_name="evidence_readiness_checks",
    )
    op.drop_index(
        "ix_evidence_readiness_checks_session_id",
        table_name="evidence_readiness_checks",
    )
    op.drop_table("evidence_readiness_checks")
