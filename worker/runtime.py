"""Окружение задач: сессия БД, хранилище, клиент LLM, ключи промежуточных файлов.

Тесты подменяют `session_scope`, `get_storage` и `get_llm` (транзакция теста, `MemoryStorage`, подделка
LLM без сети), поэтому задачи обращаются к ним через модуль: `runtime.session_scope()`, а не через
импорт имени.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy.orm import Session

from db.session import session_scope as _db_session_scope
from storage import Storage, storage_from_env


@contextmanager
def session_scope() -> Iterator[Session]:
    """Короткая транзакция: задача не держит соединение, пока работает OCR или LibreOffice."""
    with _db_session_scope() as s:
        yield s


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    return storage_from_env()


def get_llm():
    """Клиент LLM из окружения (`LLM_BASE_URL`, `LLM_MODEL`); в docker-compose — заглушка `llm-stub`."""
    from ml.analysis.llm import default_client

    return default_client()


def work_key(batch_id: str, document_id: str, name: str) -> str:
    """Промежуточные файлы документа лежат в MinIO рядом с исходниками пакета."""
    return f"batches/{batch_id}/work/{document_id}/{name}"


def unpacked_key(batch_id: str, document_id: str, ext: str) -> str:
    """Файл, распакованный из архива: ключ из id документа, повтор распаковки перезаписывает его."""
    return f"batches/{batch_id}/unpacked/{document_id}.{ext}"
