"""Compatibility marker for the existing V6 deployment database.

The yesterday baseline intentionally starts from the standalone V6 schema at
``20260804_0001``.  The server database was previously stamped at
``20260804_0003`` by a later branch.  This no-op marker lets the baseline code
load that already-compatible superset schema without importing the later
branch's application migrations or changing any tables.
"""

from __future__ import annotations


revision = "20260804_0003"
down_revision = "20260804_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
