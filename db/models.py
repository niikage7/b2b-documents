"""Таблицы PostgreSQL по «Требованиям к бэкенду» (6 октября): 13 таблиц и индексы для экранов.

Отличия от документа, которые стоит знать:

- `batch.meta` — паспорт закупки из извещения (НМЦК, обеспечения, сроки) для шапки пакета;
  `batch.subject` остаётся отдельным полем, его подставляют в правила.
- `finding.recommendation` — что поставщику сделать с риском; пустое у ручных пометок.
- `block.stream` и `block.page_end` — из сырых абзацев: сноски не образуют разделов, а
  абзац может переходить на следующую страницу.
- У `chunk` нет индекса HNSW: поиск идёт внутри одного документа, фрагменты читаются
  целиком и ранжируются в `ml.analysis.search`.
- `finding_event.actor_id` пуст у событий, которые записала система (находки ИИ).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from db.config import embedding_dim

EMBEDDING_DIM = embedding_dim()


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)


def _fk(target: str, *, nullable: bool = False, ondelete: str | None = None, index: bool = False) -> Mapped:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


def _created() -> Mapped[datetime]:
    # Время из Python, а не now() базы: now() одинаково для всей транзакции, и события одной
    # транзакции (находка и её «создана») теряли бы порядок
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                         default=lambda: datetime.now(UTC))


class Base(DeclarativeBase):
    pass


# --- Пользователи и пакеты ------------------------------------------------------------------


class User(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = _pk()
    login: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(200), unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # specialist, lawyer, methodologist
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sql_text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class Batch(Base):
    __tablename__ = "batch"
    __table_args__ = (
        Index("ix_batch_author_created", "author_id", sql_text("created_at DESC")),
        Index("ix_batch_status", "status"),
        Index("ix_batch_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    subject: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")
    profile_id: Mapped[uuid.UUID] = _fk("rule_profile.id")
    author_id: Mapped[uuid.UUID] = _fk("user.id")
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadedFile(Base):
    __tablename__ = "uploaded_file"

    id: Mapped[uuid.UUID] = _pk()
    batch_id: Mapped[uuid.UUID] = _fk("batch.id", ondelete="CASCADE", index=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(200), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    check: Mapped[str] = mapped_column(String(20), nullable=False)  # ok, unsupported, encrypted, corrupted
    message: Mapped[str | None] = mapped_column(Text)
    archive_entries: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        Index("ix_document_filename_trgm", "filename", postgresql_using="gin",
              postgresql_ops={"filename": "gin_trgm_ops"}),
    )

    id: Mapped[uuid.UUID] = _pk()
    batch_id: Mapped[uuid.UUID] = _fk("batch.id", ondelete="CASCADE", index=True)
    uploaded_file_id: Mapped[uuid.UUID] = _fk("uploaded_file.id", ondelete="CASCADE")
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # docx, doc, rtf, pdf, scan
    pages: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    stage_detail: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    worst_level: Mapped[str | None] = mapped_column(String(20))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    low_quality_pages: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list,
                                                         server_default="{}")
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRun(Base):
    __tablename__ = "task_run"
    __table_args__ = (Index("ix_task_run_document", "document_id", "started_at"),)

    id: Mapped[uuid.UUID] = _pk()
    batch_id: Mapped[uuid.UUID] = _fk("batch.id", ondelete="CASCADE")
    document_id: Mapped[uuid.UUID | None] = _fk("document.id", nullable=True, ondelete="CASCADE")
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running, done, error
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Export(Base):
    __tablename__ = "export"

    id: Mapped[uuid.UUID] = _pk()
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # document, batch
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(10), nullable=False)
    include_rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                   server_default=sql_text("false"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing",
                                        server_default="processing")
    object_key: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = _fk("user.id")
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Текст документа ----------------------------------------------------------------------


class BlockRow(Base):
    __tablename__ = "block"
    __table_args__ = (
        Index("ix_block_document_order", "document_id", "order"),
        Index("ix_block_document_page", "document_id", "page"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = _fk("document.id", ondelete="CASCADE")
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    number: Mapped[str | None] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[int | None] = mapped_column(Integer)
    align: Mapped[str | None] = mapped_column(String(10))
    bold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sql_text("false"))
    stream: Mapped[str] = mapped_column(String(20), nullable=False, default="body", server_default="body")
    section_block_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    section_number: Mapped[str | None] = mapped_column(String(100))
    section_title: Mapped[str | None] = mapped_column(Text)
    table_id: Mapped[int | None] = mapped_column(Integer)
    row: Mapped[int | None] = mapped_column(Integer)
    col: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    low_ocr_quality: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=sql_text("false"))
    source_map: Mapped[dict | None] = mapped_column(JSONB)


class ChunkRow(Base):
    __tablename__ = "chunk"

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = _fk("document.id", ondelete="CASCADE", index=True)
    block_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    page_from: Mapped[int] = mapped_column(Integer, nullable=False)
    page_to: Mapped[int] = mapped_column(Integer, nullable=False)


# --- Правила ------------------------------------------------------------------------------


class RuleProfile(Base):
    __tablename__ = "rule_profile"
    __table_args__ = (
        Index("uq_rule_profile_default", "is_default", unique=True, postgresql_where=sql_text("is_default")),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sql_text("false"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class Rule(Base):
    __tablename__ = "rule"
    __table_args__ = (
        Index("uq_rule_profile_code", "profile_id", "code", unique=True, postgresql_where=sql_text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = _fk("rule_profile.id", ondelete="CASCADE")
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sql_text("true"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_version.id", use_alter=True, name="fk_rule_current_version"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by: Mapped[uuid.UUID | None] = _fk("user.id", nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuleVersion(Base):
    __tablename__ = "rule_version"

    id: Mapped[uuid.UUID] = _pk()
    rule_id: Mapped[uuid.UUID] = _fk("rule.id", ondelete="CASCADE", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()
    created_by: Mapped[uuid.UUID | None] = _fk("user.id", nullable=True)
    query_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class BatchRule(Base):
    """Версии правил, которыми проверяется пакет: фиксируются на старте (FR-9.4*)."""

    __tablename__ = "batch_rule"

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("batch.id", ondelete="CASCADE"),
                                                primary_key=True)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("rule_version.id"),
                                                       primary_key=True)


# --- Находки ------------------------------------------------------------------------------


class Finding(Base):
    __tablename__ = "finding"
    __table_args__ = (
        Index("ix_finding_document_status", "document_id", "status"),
        Index("ix_finding_batch_rule", "batch_id", "rule_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    batch_id: Mapped[uuid.UUID] = _fk("batch.id", ondelete="CASCADE")
    document_id: Mapped[uuid.UUID] = _fk("document.id", ondelete="CASCADE")
    rule_id: Mapped[uuid.UUID | None] = _fk("rule.id", nullable=True)
    rule_version_id: Mapped[uuid.UUID | None] = _fk("rule_version.id", nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # ai, user
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    original_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", server_default="new")
    block_id: Mapped[uuid.UUID] = _fk("block.id", ondelete="CASCADE")
    start: Mapped[int] = mapped_column(Integer, nullable=False)
    end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = _fk("user.id", nullable=True)
    decided_by: Mapped[uuid.UUID | None] = _fk("user.id", nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = _created()


class FindingEvent(Base):
    __tablename__ = "finding_event"

    id: Mapped[uuid.UUID] = _pk()
    finding_id: Mapped[uuid.UUID] = _fk("finding.id", ondelete="CASCADE", index=True)
    actor_id: Mapped[uuid.UUID | None] = _fk("user.id", nullable=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    old: Mapped[dict | None] = mapped_column(JSONB)
    new: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
