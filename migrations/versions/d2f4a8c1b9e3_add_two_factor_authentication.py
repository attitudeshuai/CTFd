"""Add two factor authentication support (TOTP secret and recovery codes)

Revision ID: d2f4a8c1b9e3
Revises: a3e1c5d7b9f2
Create Date: 2026-10-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d2f4a8c1b9e3"
down_revision = "a3e1c5d7b9f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("totp_secret", sa.String(length=64), nullable=True)
    )
    op.create_table(
        "user_recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("code_hash", sa.String(length=128), nullable=True),
        sa.Column("used", sa.Boolean(), nullable=True),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("user_recovery_codes")
    op.drop_column("users", "totp_secret")
