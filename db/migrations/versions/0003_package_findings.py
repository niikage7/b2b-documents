"""Находки проверок пакета: код встроенной проверки и ссылки на другие места (FR-4.4*).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("finding", sa.Column("check_code", sa.String(20)))
    op.add_column("finding", sa.Column("check_title", sa.String(300)))
    op.add_column("finding", sa.Column("related", JSONB))


def downgrade() -> None:
    op.drop_column("finding", "related")
    op.drop_column("finding", "check_title")
    op.drop_column("finding", "check_code")
