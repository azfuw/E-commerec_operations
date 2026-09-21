"""Isolate operations and logistics accounts; existing users stay operations.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("department", sa.String(10),
        sa.CheckConstraint("department IN ('operations', 'logistics')", name="user_department"),
        server_default="operations", nullable=False))


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("user_department", "users", type_="check")
    op.drop_column("users", "department")
