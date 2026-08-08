"""Add versioned evidence-attribution spans and evidence audit bindings.

Existing evidence rows remain explicitly unclassified.  This migration never
infers participant ownership from historical quote text.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_0002"
down_revision = "20260808_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_attribution_spans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_turn_id", sa.Integer(), nullable=False),
        sa.Column("readiness_check_id", sa.Integer(), nullable=True),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("elicitation_level", sa.String(length=32), nullable=False),
        sa.Column("source_label", sa.String(length=500), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("eligibility", sa.String(length=24), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_reason", sa.Text(), nullable=True),
        sa.Column("transcript_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("asset_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("turn_index >= 0", name="ck_attribution_turn_index"),
        sa.CheckConstraint("span_start >= 0", name="ck_attribution_start"),
        sa.CheckConstraint("span_end > span_start", name="ck_attribution_end"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_attribution_confidence",
        ),
        sa.CheckConstraint(
            "owner IN ('participant_owned','external_quoted','external_paraphrased','uncertain')",
            name="ck_attribution_owner",
        ),
        sa.CheckConstraint(
            "relation IN ('own_reasoning','endorses','critiques','rejects','quotes_only','asks_or_requests')",
            name="ck_attribution_relation",
        ),
        sa.CheckConstraint(
            "elicitation_level IN ('spontaneous','open_probe','focused_probe','strong_scaffold')",
            name="ck_attribution_elicitation",
        ),
        sa.CheckConstraint(
            "eligibility IN ('eligible','context_only','manual_review')",
            name="ck_attribution_eligibility",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending','validated','rejected','manual_review','legacy_unclassified')",
            name="ck_attribution_validation_status",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assessment_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_turn_id"],
            ["dialogue_turns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["readiness_check_id"],
            ["evidence_readiness_checks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "readiness_check_id",
            "user_turn_id",
            "span_start",
            "span_end",
            name="uq_attribution_check_turn_span",
        ),
    )
    op.create_index(
        "ix_evidence_attribution_spans_session_id",
        "evidence_attribution_spans",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_attribution_spans_user_turn_id",
        "evidence_attribution_spans",
        ["user_turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_attribution_spans_readiness_check_id",
        "evidence_attribution_spans",
        ["readiness_check_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_attribution_spans_transcript_fingerprint",
        "evidence_attribution_spans",
        ["transcript_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_attribution_spans_asset_fingerprint",
        "evidence_attribution_spans",
        ["asset_fingerprint"],
        unique=False,
    )

    with op.batch_alter_table("evidence_items") as batch_op:
        batch_op.add_column(
            sa.Column("attribution_span_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("readiness_check_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "validation_status",
                sa.String(length=32),
                nullable=False,
                server_default="legacy_unclassified",
            )
        )
        batch_op.add_column(
            sa.Column("validation_reason", sa.Text(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_evidence_items_attribution_span_id",
            "evidence_attribution_spans",
            ["attribution_span_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_evidence_items_readiness_check_id",
            "evidence_readiness_checks",
            ["readiness_check_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_evidence_validation_status",
            "validation_status IN ('pending','validated','rejected','manual_review','legacy_unclassified')",
        )
        batch_op.create_check_constraint(
            "ck_evidence_validated_attribution",
            "validation_status != 'validated' OR attribution_span_id IS NOT NULL",
        )
        batch_op.create_index(
            "ix_evidence_items_attribution_span_id",
            ["attribution_span_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_evidence_items_readiness_check_id",
            ["readiness_check_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence_items") as batch_op:
        batch_op.drop_constraint(
            "ck_evidence_validated_attribution",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_evidence_validation_status",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_evidence_items_readiness_check_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_evidence_items_attribution_span_id",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_evidence_items_readiness_check_id")
        batch_op.drop_index("ix_evidence_items_attribution_span_id")
        batch_op.drop_column("validation_reason")
        batch_op.drop_column("validation_status")
        batch_op.drop_column("readiness_check_id")
        batch_op.drop_column("attribution_span_id")

    op.drop_index(
        "ix_evidence_attribution_spans_asset_fingerprint",
        table_name="evidence_attribution_spans",
    )
    op.drop_index(
        "ix_evidence_attribution_spans_transcript_fingerprint",
        table_name="evidence_attribution_spans",
    )
    op.drop_index(
        "ix_evidence_attribution_spans_readiness_check_id",
        table_name="evidence_attribution_spans",
    )
    op.drop_index(
        "ix_evidence_attribution_spans_user_turn_id",
        table_name="evidence_attribution_spans",
    )
    op.drop_index(
        "ix_evidence_attribution_spans_session_id",
        table_name="evidence_attribution_spans",
    )
    op.drop_table("evidence_attribution_spans")
