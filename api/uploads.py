"""Приём файлов в черновик пакета: запись на диск, проверка, MinIO (FR-1.1, FR-1.2).

Zip не распаковывается: показываем оглавление из центрального каталога архива. Распаковку с защитой
от zip-бомбы делает задача `unpack_batch` при запуске пакета.
"""

from __future__ import annotations

import mimetypes
import shutil
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ml.errors import UnsupportedFormatError
from ml.files import check_file, sniff_kind
from worker.archive import entry_name, is_service_entry, is_unsafe

MAX_FILENAME = 500
MAX_ARCHIVE_ENTRIES = 5000
UNSUPPORTED_MESSAGE = UnsupportedFormatError.default_message
CORRUPTED_MESSAGE = "Файл повреждён и не открывается — он не будет проверен"


def clean_filename(raw: str | None) -> str:
    """Имя без пути (браузеры иногда присылают полный путь Windows), не длиннее колонки в БД."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip().strip(".")
    name = "".join(ch for ch in name if ch.isprintable()) or "file"
    if len(name) > MAX_FILENAME:
        suffix = PurePosixPath(name).suffix[:20]
        name = name[: MAX_FILENAME - len(suffix)] + suffix
    return name


def extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lstrip(".").lower()


def object_key(batch_id: str, filename: str) -> str:
    """Ключ из случайного id: имя файла из запроса в ключ не попадает (нет path traversal и коллизий)."""
    return f"batches/{batch_id}/source/{uuid.uuid4().hex}{PurePosixPath(filename).suffix.lower()[:12]}"


def save_to_disk(src: BinaryIO, dest: Path) -> int:
    """Потоковая запись во временный файл; возвращает размер."""
    with dest.open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    return dest.stat().st_size


def archive_entries(path: Path, allowed: Iterable[str]) -> list[dict]:
    """Оглавление zip без распаковки: путь, размер и результат проверки по расширению.

    Вложенный zip — `ok`: его содержимое распакует и проверит `unpack_batch`.
    """
    ok_ext = set(allowed)
    entries: list[dict] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = entry_name(info)
            if is_service_entry(name):
                continue
            if len(entries) >= MAX_ARCHIVE_ENTRIES:
                break
            if is_unsafe(name) or extension(name) not in ok_ext:
                check = "unsupported"
            elif info.flag_bits & 0x1:
                check = "encrypted"
            else:
                check = "ok"
            entries.append({"path": name, "size_bytes": info.file_size, "check": check})
    return entries


def inspect_file(path: Path, filename: str, allowed: Iterable[str]) -> dict:
    """`check`, `message`, `archive_entries` для строки `uploaded_file`."""
    allowed = tuple(allowed)
    if extension(filename) not in allowed:
        return {"check": "unsupported", "message": UNSUPPORTED_MESSAGE, "archive_entries": None}
    if extension(filename) == "zip" and _has_zip_magic(path):
        # Не через check_file: он ищет «%PDF-» в первом килобайте раньше сигнатуры zip, а в архиве без сжатия
        # первым может лежать PDF, и архив опознался бы как PDF
        return _inspect_archive(path, allowed)
    result = check_file(path)
    if result.check == "unsupported" and sniff_kind(path) is None:
        # Расширение допустимое, а содержимое не похоже ни на один формат: файл повреждён или подменён
        return {"check": "corrupted", "message": CORRUPTED_MESSAGE, "archive_entries": None}
    if result.check == "ok" and result.kind == "zip":
        return _inspect_archive(path, allowed)
    return {"check": result.check, "message": result.message, "archive_entries": None}


def _has_zip_magic(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) in (b"PK", b"PK")


def _inspect_archive(path: Path, allowed: tuple[str, ...]) -> dict:
    try:
        entries = archive_entries(path, allowed)
    except (zipfile.BadZipFile, OSError, NotImplementedError):
        return {"check": "corrupted", "message": CORRUPTED_MESSAGE, "archive_entries": None}
    return {"check": "ok", "message": None, "archive_entries": entries}


def guess_mime(filename: str, declared: str | None) -> str:
    return declared or mimetypes.guess_type(filename)[0] or "application/octet-stream"
