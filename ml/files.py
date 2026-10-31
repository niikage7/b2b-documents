"""Проверка файла при загрузке (FR-1.2): формат по сигнатуре, пароль, повреждение.

Работает синхронно и без извлечения текста: её зовёт API на шаге загрузки черновика.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import olefile
import pymupdf
from lxml import etree

from contracts.extraction import FileCheck, FileKind
from ml.errors import (
    CorruptedFileError,
    EncryptedFileError,
    ExtractionError,
    UnsupportedFormatError,
)

log = logging.getLogger(__name__)

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
MAX_PART_BYTES = 300 * 1024 * 1024
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def sniff_kind(path: str | Path) -> FileKind | None:
    """Тип файла по сигнатуре, без разбора содержимого. OLE считается DOC, zip — архивом."""
    with Path(path).open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" in head:
        return "pdf"
    if head.startswith(_ZIP_MAGICS):
        return "zip"
    if head.startswith(_OLE_MAGIC):
        return "doc"
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{\\rtf"):
        return "rtf"
    return None


def check_file(path: str | Path) -> FileCheck:
    """Проверяет файл и возвращает результат для `uploaded_file.check`.

    На плохих файлах не бросает исключений: ошибка превращается в `check` с причиной
    на русском. `pages` заполняется только для PDF.
    """
    path = Path(path)
    if not path.is_file():
        return _fail(CorruptedFileError("Файл не найден"), None)
    if path.stat().st_size == 0:
        return _fail(CorruptedFileError("Файл пустой"), None)
    kind = sniff_kind(path)
    try:
        if kind == "pdf":
            return _check_pdf(path)
        if kind == "zip":
            return _check_zip(path)
        if kind == "doc":
            return _check_ole(path)
        if kind == "rtf":
            return FileCheck(check="ok", kind="rtf")
        raise UnsupportedFormatError()
    except ExtractionError as exc:
        return _fail(exc, kind, path)


def require_ok(path: str | Path) -> FileCheck:
    """То же, что `check_file`, но бросает исключение с кодом: для этапа извлечения."""
    result = check_file(path)
    if result.check == "ok":
        return result
    exc_type = {
        "unsupported": UnsupportedFormatError,
        "encrypted": EncryptedFileError,
        "corrupted": CorruptedFileError,
    }[result.check]
    raise exc_type(result.message)


def _fail(exc: ExtractionError, kind: FileKind | None, path: Path | None = None) -> FileCheck:
    name = path.name if path else "?"
    log.info("check_file %s: %s (%s)", name, exc.code, exc.detail or exc.message)
    status = exc.code if exc.code in ("unsupported", "encrypted", "corrupted") else "corrupted"
    kind = getattr(exc, "kind", kind)
    return FileCheck(check=status, kind=None if status == "unsupported" else kind, message=exc.message)


def _check_pdf(path: Path) -> FileCheck:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF бросает разные типы на битых файлах
        raise CorruptedFileError(detail=f"pdf: {exc}") from exc
    try:
        if doc.needs_pass:
            raise EncryptedFileError()
        try:
            pages = doc.page_count
        except Exception as exc:  # у обрезанного файла open проходит, а дерево страниц не читается
            raise CorruptedFileError(detail=f"pdf: {exc}") from exc
        if pages == 0:
            raise CorruptedFileError("В файле PDF нет страниц")
        return FileCheck(check="ok", kind="pdf", pages=pages)
    finally:
        doc.close()


def _check_zip(path: Path) -> FileCheck:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                if zf.getinfo("word/document.xml").file_size > MAX_PART_BYTES:
                    err = CorruptedFileError("Файл DOCX слишком большой для разбора")
                    err.kind = "docx"
                    raise err
                try:
                    etree.fromstring(zf.read("word/document.xml"), _PARSER)
                except (etree.XMLSyntaxError, zipfile.BadZipFile, KeyError, OSError) as exc:
                    err = CorruptedFileError(detail=f"docx: {exc}")
                    err.kind = "docx"
                    raise err from exc
                return FileCheck(check="ok", kind="docx")
            if "[Content_Types].xml" in names or "mimetype" in names:
                # Другие форматы Office (xlsx, pptx) и OpenDocument в пакете не проверяются
                raise UnsupportedFormatError()
            return FileCheck(check="ok", kind="zip")
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError(detail=f"zip: {exc}") from exc


def doc_page_count(path: str | Path) -> int | None:
    """Число страниц DOC из свойств документа Word (SummaryInformation), если Word его записал."""
    try:
        ole = olefile.OleFileIO(str(path))
    except OSError:
        return None
    try:
        pages = ole.get_metadata().num_pages
    except Exception:  # noqa: BLE001 — повреждённые свойства не мешают разбору текста
        log.debug("doc: свойства документа не прочитаны: %s", path)
        return None
    finally:
        ole.close()
    return pages if isinstance(pages, int) and pages > 0 else None


def _check_ole(path: Path) -> FileCheck:
    try:
        ole = olefile.OleFileIO(str(path))
    except OSError as exc:
        raise CorruptedFileError(detail=f"ole: {exc}") from exc
    try:
        if ole.exists("EncryptedPackage"):
            # DOCX с паролем хранится как контейнер OLE с зашифрованным пакетом внутри
            err = EncryptedFileError()
            err.kind = "docx"
            raise err
        if not ole.exists("WordDocument"):
            raise UnsupportedFormatError()
        fib = ole.openstream("WordDocument").read(12)
        if len(fib) < 12:
            raise CorruptedFileError(detail="doc: короткий FIB")
        flags = int.from_bytes(fib[10:12], "little")
        if flags & 0x0100:  # FibBase.fEncrypted
            raise EncryptedFileError()
        return FileCheck(check="ok", kind="doc")
    finally:
        ole.close()
