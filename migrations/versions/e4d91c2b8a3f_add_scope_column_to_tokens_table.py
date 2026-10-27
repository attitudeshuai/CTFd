"""Add scope column to tokens table

Revision ID: e4d91c2b8a3f
Revises: a3e1c5d7b9f2
Create Date: 2026-10-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4d91c2b8a3f"
down_revision = "a3e1c5d7b9f2"
branch_labels = None
depends_on = None


def upgrade():
    # The server_default backfills all existing tokens with full scope so that
    # legacy tokens keep their previous unrestricted behavior.
    op.add_column(
        "tokens",
        sa.Column(
            "scope",
            sa.String(length=32),
            nullable=False,
            server_default="full",
        ),
    )


def downgrade():
    op.drop_column("tokens", "scope")
