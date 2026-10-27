"""Add notification_reads

Revision ID: c4f7a2e91b3d
Revises: a3e1c5d7b9f2
Create Date: 2026-10-26 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4f7a2e91b3d"
down_revision = "a3e1c5d7b9f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_reads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "notification_id", "user_id", name="uq_notification_read_user"
        ),
    )
    op.create_index(
        "ix_notification_reads_notification_id",
        "notification_reads",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_reads_user_id",
        "notification_reads",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_notification_reads_user_id", table_name="notification_reads")
    op.drop_index(
        "ix_notification_reads_notification_id", table_name="notification_reads"
    )
    op.drop_table("notification_reads")
