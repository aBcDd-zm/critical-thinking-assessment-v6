"""Persist the frozen final-scorer contract digest per scoring run.

Revision ID: 20260804_0003
Revises: 20260804_0002
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0003"
down_revision = "20260804_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical rows remain NULL because their exact runtime contract cannot
    # be proven retroactively. New scoring runs always write the frozen digest.
    op.add_column(
        "scoring_runs",
        sa.Column(
            "final_scorer_contract_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("scoring_runs", "final_scorer_contract_sha256")
