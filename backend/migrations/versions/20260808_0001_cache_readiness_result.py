"""Cache validated readiness output for exact-transcript finalization reuse."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_0001"
down_revision = "20260806_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_readiness_checks",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "evidence_readiness_checks",
        sa.Column("last_user_turn_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "evidence_readiness_checks",
        sa.Column("result_data", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidence_readiness_checks", "result_data")
    op.drop_column("evidence_readiness_checks", "last_user_turn_index")
    op.drop_column("evidence_readiness_checks", "attempt_count")
