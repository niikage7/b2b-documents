"""Окно длинного блока у фрагмента: `chunk.char_start`, `chunk.char_end`.

Блок длиннее `LONG_BLOCK_TOKENS` (ячейка таблицы на тысячи токенов) режется для поиска на
окна по предложениям; LLM получает только найденные окна. У фрагментов из целых блоков
оба поля пустые, поэтому старые строки переиндексировать не нужно.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27 18:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunk", sa.Column("char_start", sa.Integer(), nullable=True))
    op.add_column("chunk", sa.Column("char_end", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunk", "char_end")
    op.drop_column("chunk", "char_start")
