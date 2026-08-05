"""Persist verified model identity and non-content call provenance.

Revision ID: 20260804_0002
Revises: 20260804_0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0002"
down_revision = "20260804_0001"
branch_labels = None
depends_on = None


_TABLES = ("agent_traces", "scoring_runs")


def upgrade() -> None:
    for table_name in _TABLES:
        op.add_column(
            table_name,
            sa.Column("requested_model", sa.String(length=120), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("actual_model", sa.String(length=120), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("response_id", sa.String(length=160), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("request_id", sa.String(length=160), nullable=True),
        )
        op.add_column(table_name, sa.Column("prompt_tokens", sa.Integer(), nullable=True))
        op.add_column(
            table_name,
            sa.Column("completion_tokens", sa.Integer(), nullable=True),
        )
        op.add_column(table_name, sa.Column("total_tokens", sa.Integer(), nullable=True))
        op.add_column(
            table_name,
            sa.Column(
                "transport_retry_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )


def downgrade() -> None:
    for table_name in reversed(_TABLES):
        op.drop_column(table_name, "transport_retry_count")
        op.drop_column(table_name, "total_tokens")
        op.drop_column(table_name, "completion_tokens")
        op.drop_column(table_name, "prompt_tokens")
        op.drop_column(table_name, "request_id")
        op.drop_column(table_name, "response_id")
        op.drop_column(table_name, "actual_model")
        op.drop_column(table_name, "requested_model")
