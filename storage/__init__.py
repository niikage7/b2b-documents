"""Объектное хранилище (MinIO, S3-совместимый API): исходные файлы, результаты извлечения, экспорт.

Общий модуль API и воркеров. Клиент создаётся лениво, бакет — при первом обращении или в `ensure_bucket`.
Тесты подставляют `MemoryStorage` вместо настоящего MinIO.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class Storage(Protocol):
    def put_file(self, key: str, path: str | Path, *, content_type: str = "application/octet-stream") -> None: ...

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def download(self, key: str, path: str | Path) -> None: ...

    def stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]: ...

    def delete(self, keys: Iterable[str]) -> None: ...

    def ensure_bucket(self) -> None: ...


class MinioStorage:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, *, secure: bool = False):
        from minio import Minio

        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket
        self._ready = False
        self._lock = threading.Lock()

    def ensure_bucket(self) -> None:
        with self._lock:
            if self._ready:
                return
            if not self._client.bucket_exists(self.bucket):
                try:
                    self._client.make_bucket(self.bucket)
                except Exception:  # noqa: BLE001 — параллельный запуск мог создать бакет раньше
                    if not self._client.bucket_exists(self.bucket):
                        raise
            self._ready = True

    def put_file(self, key: str, path: str | Path, *, content_type: str = "application/octet-stream") -> None:
        self.ensure_bucket()
        self._client.fput_object(self.bucket, key, str(path), content_type=content_type)

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        import io

        self.ensure_bucket()
        self._client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        self.ensure_bucket()
        response = self._client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def download(self, key: str, path: str | Path) -> None:
        self.ensure_bucket()
        self._client.fget_object(self.bucket, key, str(path))

    def stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Объект кусками: API отдаёт выгрузку браузеру, не держа файл в памяти."""
        self.ensure_bucket()
        response = self._client.get_object(self.bucket, key)
        try:
            yield from response.stream(chunk_size)
        finally:
            response.close()
            response.release_conn()

    def delete(self, keys: Iterable[str]) -> None:
        """Удаляет объекты; сбой отдельного ключа не прерывает остальные и не бросается наружу."""
        for key in keys:
            try:
                self._client.remove_object(self.bucket, key)
            except Exception:  # noqa: BLE001 — лишний объект в хранилище не должен ронять запрос
                log.warning("MinIO: не удалось удалить %s", key, exc_info=True)


class MemoryStorage:
    """Хранилище в памяти для тестов."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put_file(self, key: str, path: str | Path, *, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = Path(path).read_bytes()

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def download(self, key: str, path: str | Path) -> None:
        Path(path).write_bytes(self.objects[key])

    def stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        data = self.objects[key]
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def delete(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)


def storage_from_env() -> MinioStorage:
    """Клиент по переменным `MINIO_*` — так его создают и API, и воркеры."""
    return MinioStorage(
        os.environ.get("MINIO_ENDPOINT") or "localhost:9000",
        os.environ.get("MINIO_ACCESS_KEY") or "b2b-minio",
        os.environ.get("MINIO_SECRET_KEY") or "b2b-minio-secret",
        os.environ.get("MINIO_BUCKET") or "b2b-documents",
        secure=(os.environ.get("MINIO_SECURE") or "").strip().lower() in {"1", "true", "yes", "on"},
    )
