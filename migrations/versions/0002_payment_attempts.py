"""Allow several payment attempts per booking, at most one successful.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

0001 made payments.booking_id unique, so a declined payment blocked every retry for that
booking. A partial unique index keeps the guarantee that matters (a booking can never be
paid twice) while letting failed attempts be recorded.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_payments_booking_id", "payments", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_payments_one_success_per_booking "
        "ON payments (booking_id) WHERE status = 'succeeded'"
    )


def downgrade() -> None:
    op.drop_index("uq_payments_one_success_per_booking", table_name="payments")
    op.create_unique_constraint("uq_payments_booking_id", "payments", ["booking_id"])
