"""Начальная схема: таблицы «Требований к бэкенду», `batch_rule` и расширения vector и pg_trgm.

Размерность `vector(N)` берётся из `EMBEDDING_DIM` (по умолчанию 768): она должна совпадать с
моделью эмбеддингов. Сменить модель с другой размерностью — новая миграция и переиндексация.

Revision ID: 0001
Revises:
Create Date: 2026-10-27 02:39:58.698969
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from db.config import embedding_dim

EMBEDDING_DIM = embedding_dim()

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('rule_profile',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('short_name', sa.String(length=100), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_rule_profile_default', 'rule_profile', ['is_default'], unique=True, postgresql_where=sa.text('is_default'))
    op.create_table('user',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('login', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('failed_logins', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('login')
    )
    op.create_table('batch',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=300), server_default='', nullable=False),
    sa.Column('subject', sa.Text(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('profile_id', sa.UUID(), nullable=False),
    sa.Column('author_id', sa.UUID(), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('eta_seconds', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['profile_id'], ['rule_profile.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_batch_author_created', 'batch', ['author_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_batch_name_trgm', 'batch', ['name'], unique=False, postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('ix_batch_status', 'batch', ['status'], unique=False)
    op.create_table('export',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('target_id', sa.UUID(), nullable=False),
    sa.Column('format', sa.String(length=10), nullable=False),
    sa.Column('include_rejected', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='processing', nullable=False),
    sa.Column('object_key', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_export_target_id'), 'export', ['target_id'], unique=False)
    op.create_table('rule',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('profile_id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('level', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('prompt_template', sa.Text(), nullable=False),
    sa.Column('variables', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('current_version_id', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['profile_id'], ['rule_profile.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_rule_profile_code', 'rule', ['profile_id', 'code'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('rule_version',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rule_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('query_embedding', Vector(EMBEDDING_DIM), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['rule_id'], ['rule.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rule_version_rule_id'), 'rule_version', ['rule_id'], unique=False)
    # rule и rule_version ссылаются друг на друга: вторая ссылка — после создания обеих таблиц
    op.create_foreign_key('fk_rule_current_version', 'rule', 'rule_version', ['current_version_id'], ['id'])
    op.create_table('uploaded_file',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('mime', sa.String(length=200), nullable=False),
    sa.Column('object_key', sa.Text(), nullable=False),
    sa.Column('check', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('archive_entries', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_uploaded_file_batch_id'), 'uploaded_file', ['batch_id'], unique=False)
    op.create_table('batch_rule',
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('rule_version_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_version_id'], ['rule_version.id'], ),
    sa.PrimaryKeyConstraint('batch_id', 'rule_version_id')
    )
    op.create_table('document',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('uploaded_file_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('original_path', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('pages', sa.Integer(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='queued', nullable=False),
    sa.Column('stage', sa.String(length=20), server_default='queued', nullable=False),
    sa.Column('stage_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('progress', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('worst_level', sa.String(length=20), nullable=True),
    sa.Column('ocr_confidence', sa.Float(), nullable=True),
    sa.Column('low_quality_pages', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False),
    sa.Column('object_key', sa.Text(), nullable=False),
    sa.Column('normalized_key', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_file_id'], ['uploaded_file.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_batch_id'), 'document', ['batch_id'], unique=False)
    op.create_index('ix_document_filename_trgm', 'document', ['filename'], unique=False, postgresql_using='gin', postgresql_ops={'filename': 'gin_trgm_ops'})
    op.create_table('block',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('number', sa.String(length=100), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('page_end', sa.Integer(), nullable=True),
    sa.Column('level', sa.Integer(), nullable=True),
    sa.Column('align', sa.String(length=10), nullable=True),
    sa.Column('bold', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('stream', sa.String(length=20), server_default='body', nullable=False),
    sa.Column('section_block_id', sa.UUID(), nullable=True),
    sa.Column('section_number', sa.String(length=100), nullable=True),
    sa.Column('section_title', sa.Text(), nullable=True),
    sa.Column('table_id', sa.Integer(), nullable=True),
    sa.Column('row', sa.Integer(), nullable=True),
    sa.Column('col', sa.Integer(), nullable=True),
    sa.Column('bbox', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('low_ocr_quality', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('source_map', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_block_document_order', 'block', ['document_id', 'order'], unique=False)
    op.create_index('ix_block_document_page', 'block', ['document_id', 'page'], unique=False)
    op.create_table('chunk',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('block_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('embedding', Vector(EMBEDDING_DIM), nullable=False),
    sa.Column('page_from', sa.Integer(), nullable=False),
    sa.Column('page_to', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chunk_document_id'), 'chunk', ['document_id'], unique=False)
    op.create_table('task_run',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=True),
    sa.Column('stage', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempt', sa.Integer(), server_default='1', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_task_run_document', 'task_run', ['document_id', 'started_at'], unique=False)
    op.create_table('finding',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('rule_id', sa.UUID(), nullable=True),
    sa.Column('rule_version_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.Column('level', sa.String(length=20), nullable=False),
    sa.Column('original_level', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='new', nullable=False),
    sa.Column('block_id', sa.UUID(), nullable=False),
    sa.Column('start', sa.Integer(), nullable=False),
    sa.Column('end', sa.Integer(), nullable=False),
    sa.Column('quote', sa.Text(), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=False),
    sa.Column('recommendation', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('author_id', sa.UUID(), nullable=True),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['block_id'], ['block.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['decided_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['document_id'], ['document.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['rule.id'], ),
    sa.ForeignKeyConstraint(['rule_version_id'], ['rule_version.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_finding_batch_rule', 'finding', ['batch_id', 'rule_id'], unique=False)
    op.create_index('ix_finding_document_status', 'finding', ['document_id', 'status'], unique=False)
    op.create_table('finding_event',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('finding_id', sa.UUID(), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('old', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('new', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['finding_id'], ['finding.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_finding_event_finding_id'), 'finding_event', ['finding_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_finding_event_finding_id'), table_name='finding_event')
    op.drop_table('finding_event')
    op.drop_index('ix_finding_document_status', table_name='finding')
    op.drop_index('ix_finding_batch_rule', table_name='finding')
    op.drop_table('finding')
    op.drop_index('ix_task_run_document', table_name='task_run')
    op.drop_table('task_run')
    op.drop_index(op.f('ix_chunk_document_id'), table_name='chunk')
    op.drop_table('chunk')
    op.drop_index('ix_block_document_page', table_name='block')
    op.drop_index('ix_block_document_order', table_name='block')
    op.drop_table('block')
    op.drop_index('ix_document_filename_trgm', table_name='document', postgresql_using='gin', postgresql_ops={'filename': 'gin_trgm_ops'})
    op.drop_index(op.f('ix_document_batch_id'), table_name='document')
    op.drop_table('document')
    op.drop_table('batch_rule')
    op.drop_index(op.f('ix_uploaded_file_batch_id'), table_name='uploaded_file')
    op.drop_table('uploaded_file')
    op.drop_constraint('fk_rule_current_version', 'rule', type_='foreignkey')
    op.drop_index(op.f('ix_rule_version_rule_id'), table_name='rule_version')
    op.drop_table('rule_version')
    op.drop_index('uq_rule_profile_code', table_name='rule', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('rule')
    op.drop_index(op.f('ix_export_target_id'), table_name='export')
    op.drop_table('export')
    op.drop_index('ix_batch_status', table_name='batch')
    op.drop_index('ix_batch_name_trgm', table_name='batch', postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.drop_index('ix_batch_author_created', table_name='batch')
    op.drop_table('batch')
    op.drop_table('user')
    op.drop_index('uq_rule_profile_default', table_name='rule_profile', postgresql_where=sa.text('is_default'))
    op.drop_table('rule_profile')
