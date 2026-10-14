"""Indexes for query paths not already covered by constraint indexes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14

Checked with EXPLAIN ANALYZE on the seeded dataset (NOTES.md, Milestone 3):
- Availability / overlap checks: served by the GiST index behind ex_bookings_no_overlap.
- /bookings/me: served by uq_bookings_user_id_idempotency_key (user_id is its first column).
- Hotel city filter: hotels is a few pages; the planner ignored an index on lower(city).
- Worker sweep for lapsed holds: sequential scan of all bookings without this index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Partial: only live holds are indexed, so it stays tiny however many bookings exist.
    op.create_index(
        "ix_bookings_held_expiry",
        "bookings",
        ["hold_expires_at"],
        postgresql_where=sa.text("status = 'held'"),
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_held_expiry", table_name="bookings")
