"""Безопасная распаковка zip для `unpack_batch` и оглавление архива для загрузки.

Имена записей архива никогда не становятся путями на диске: каждый файл пишется во временный
файл со случайным именем, поэтому path traversal невозможен по построению. Записи с опасным путём
(`..`, абсолютный путь, буква диска) всё равно пропускаются — так же их помечает оглавление при загрузке.

Защита от zip-бомбы считает реально распакованные байты, а не размеры из заголовков архива:
заголовки можно подделать. Лимиты — суммарный объём, число файлов и глубина вложенных архивов.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

# Форматы документов внутри архива; вложенные zip распаковываются рекурсивно
DOCUMENT_EXTENSIONS = ("pdf", "docx", "doc", "rtf")
CHUNK = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 500 * 1024 * 1024
    max_files: int = 500
    max_depth: int = 3

    @classmethod
    def from_env(cls) -> Limits:
        return cls(
            max_bytes=_env_int("UNPACK_MAX_MB", 500) * 1024 * 1024,
            max_files=_env_int("UNPACK_MAX_FILES", 500),
            max_depth=_env_int("UNPACK_MAX_DEPTH", 3),
        )


def entry_name(info: zipfile.ZipInfo) -> str:
    """Имя записи: без флага UTF-8 Python читает его как cp437. Исходные байты — это UTF-8 (`zip` на macOS
    и Linux флаг не ставит) или cp866 (русская Windows); случайно валидный UTF-8 из cp866 практически невозможен."""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            raw = name.encode("cp437")
        except UnicodeEncodeError:
            pass
        else:
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                name = raw.decode("cp866", errors="replace")
    return name.replace("\\", "/")


def is_unsafe(name: str) -> bool:
    return name.startswith("/") or ".." in name.split("/") or (len(name) > 1 and name[1] == ":")


def is_service_entry(name: str) -> bool:
    """Каталоги и служебные файлы macOS: в оглавление и в пакет не попадают."""
    return name.endswith("/") or name.startswith("__MACOSX/") or PurePosixPath(name).name == ".DS_Store"


def extension(name: str) -> str:
    return PurePosixPath(name).suffix.lstrip(".").lower()


class _CallbackFailed(Exception):
    """Сбой `on_file` (хранилище, БД) — не повреждение архива: пробрасывается наружу как есть."""


class LimitExceeded(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Problem:
    """Архив, который распакован не полностью: показывается в пакете документом с ошибкой."""

    path: str
    code: str
    message: str


@dataclass
class Unpacker:
    """Рекурсивная распаковка с общими лимитами на все архивы пакета.

    `on_file(original_path, local_path, size)` получает каждый файл документа сразу после распаковки
    и отвечает за него (загрузить в хранилище, удалить с диска): на диске одновременно лежит один файл.
    """

    workdir: Path
    on_file: Callable[[str, Path, int], None]
    limits: Limits = field(default_factory=Limits)
    total_bytes: int = 0
    files: int = 0
    problems: list[Problem] = field(default_factory=list)

    def unpack(self, archive: Path, *, label: str, prefix: str = "", depth: int = 1) -> None:
        """Распаковать `archive`; `label` — путь архива для сообщений, `prefix` — папка его файлов."""
        try:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    self._entry(zf, info, label=label, prefix=prefix, depth=depth)
        except _CallbackFailed as exc:
            if depth > 1:
                raise
            raise exc.__cause__ from None
        except LimitExceeded as exc:
            if depth > 1:
                raise  # лимит общий для пакета: распаковка останавливается целиком
            self.problems.append(Problem(label, "archive_limit", exc.message))
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError) as exc:
            log.warning("unpack: архив %s не читается: %s", label, exc)
            self.problems.append(Problem(label, "corrupted", "Архив повреждён — проверены только файлы, "
                                                              "которые удалось распаковать"))

    def _entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, label: str, prefix: str, depth: int) -> None:
        name = entry_name(info)
        if is_service_entry(name):
            return
        if is_unsafe(name):
            log.warning("unpack: %s: пропущена запись с опасным путём %r", label, name)
            return
        ext = extension(name)
        if info.flag_bits & 0x1:
            log.info("unpack: %s: запись %s защищена паролем, пропущена", label, name)
            return
        path = prefix + name
        if ext == "zip":
            nested_label = f"{label}/{name}"
            if depth >= self.limits.max_depth:
                self.problems.append(Problem(nested_label, "archive_depth",
                                             f"Вложенность архивов больше {self.limits.max_depth} уровней — "
                                             "архив не распакован"))
                return
            tmp = self._extract(zf, info, count_file=False)
            try:
                self.unpack(tmp, label=nested_label, prefix=str(PurePosixPath(path).with_suffix("")) + "/",
                            depth=depth + 1)
            finally:
                tmp.unlink(missing_ok=True)
            return
        if ext not in DOCUMENT_EXTENSIONS:
            return
        tmp = self._extract(zf, info, count_file=True)
        try:
            self.on_file(path, tmp, tmp.stat().st_size)
        except Exception as exc:
            raise _CallbackFailed() from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _extract(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, count_file: bool) -> Path:
        if count_file:
            if self.files >= self.limits.max_files:
                raise LimitExceeded(f"Архив распакован не полностью: в пакете больше {self.limits.max_files} файлов")
            self.files += 1
        fd, raw = tempfile.mkstemp(dir=self.workdir, prefix=uuid.uuid4().hex[:8], suffix=".part")
        dest = Path(raw)
        try:
            with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                while chunk := src.read(CHUNK):
                    self.total_bytes += len(chunk)
                    if self.total_bytes > self.limits.max_bytes:
                        raise LimitExceeded(
                            "Архив распакован не полностью: объём файлов после распаковки больше "
                            f"{self.limits.max_bytes // (1024 * 1024)} МБ")
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return dest
