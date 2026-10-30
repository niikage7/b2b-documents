"""Задачи выгрузки (FR-7.1, FR-7.2, FR-8.1–8.3), очередь `export`.

    export_document(export_id)                       документ в его формате: DOCX (и DOC, RTF) или PDF
    export_batch(export_id)                          заменяет себя на chord документов → build_batch_zip
    export_batch_document(export_id, document_id)    один документ пакета → exports/{id}/docs/
    build_batch_zip(results, export_id)              zip со структурой исходного архива и `Сводка.xlsx`

Строку `export` создаёт API (`db.exports.create_export`), задача пишет результат в MinIO и закрывает
строку `finish_export`; повтор задачи перезаписывает тот же ключ. Готовый файл (или ошибка) — событие
`export.done` в поток пакета, его данные — те же, что у `GET /api/exports/{id}` (`public_view`).

Файл отдаёт API: `GET /api/exports/{id}/download` (тот же источник, cookie входа), поэтому MinIO не нужно
открывать браузеру и подписанные ссылки не нужны.

Надёжность: временный сбой (БД, MinIO, Redis) повторяется с паузой, как этапы конвейера; после последней
попытки и при любой другой ошибке выгрузка получает статус `error` с причиной по-русски, а запись уходит
в dead-letter. Документ пакета, который не удалось разметить, кладётся в архив без разметки, и это
отмечено на листе «Итоги».
"""

from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from celery import chord
from sqlalchemy import select

from db import exports
from db.models import Batch, Document, Export
from db.pipeline import as_uuid
from worker import events, jsonlog, runtime
from worker.app import celery_app
from worker.tasks import DOCX_MIME, MAX_ATTEMPTS, is_retryable, retry_delay

log = logging.getLogger(__name__)

MIME = {"docx": DOCX_MIME, "pdf": "application/pdf", "zip": "application/zip",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
SUMMARY_NAME = "Сводка.xlsx"
EXPORT_FAILED = "Не удалось подготовить файл — попробуйте скачать ещё раз"
NOT_MARKED = "Не удалось разметить, в архиве исходный файл"


class ExportError(Exception):
    """Ошибка выгрузки с причиной для пользователя."""


def result_key(export_id: str, fmt: str) -> str:
    return f"exports/{export_id}/result.{fmt}"


def part_key(export_id: str, document_id: str, fmt: str) -> str:
    return f"exports/{export_id}/docs/{document_id}.{fmt}"


def download_url(export_id: str) -> str:
    return f"/api/exports/{export_id}/download"


def safe_filename(name: str | None) -> str | None:
    """Имя файла без символов, запрещённых в Windows: название пакета «Этап 7: выгрузка» → «Этап 7_ выгрузка»."""
    if name is None:
        return None
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "выгрузка"


def public_view(view: dict) -> dict:
    """`GET /api/exports/{id}` и событие `export.done` (`ExportStatus` в `frontend/src/api/types.ts`)."""
    done = view["status"] == "done" and bool(view.get("object_key"))
    return {"export_id": view["export_id"], "status": view["status"],
            "download_url": download_url(view["export_id"]) if done else None,
            "filename": safe_filename(view["filename"]), "error_message": view["error_message"], "expires_at": view["expires_at"]}


# --- Обвязка ---------------------------------------------------------------------------------


def _finish(export_id: str, *, key: str | None = None, error: str | None = None) -> dict:
    with runtime.session_scope() as s:
        view = exports.finish_export(s, export_id, object_key=key, error=error)
        export = s.get(Export, as_uuid(export_id))
        if export.scope == "batch":
            batch_id = export.target_id
        else:
            batch_id = s.get(Document, export.target_id).batch_id
    data = public_view(view)
    events.publish(str(batch_id), "export.done", data)
    return data


def _run_export(task, export_id: str, work: Callable[[], Any]) -> Any:
    """Временный сбой — повтор с паузой; остальное и последняя неудача — выгрузка `error` и dead-letter."""
    try:
        return work()
    except Exception as exc:
        attempt = task.request.retries + 1
        if is_retryable(exc) and attempt < MAX_ATTEMPTS:
            delay = retry_delay(task.request.retries)
            log.warning("%s: выгрузка %s: попытка %d из %d не удалась (%r), повтор через %.0f с",
                        task.name, export_id, attempt, MAX_ATTEMPTS, exc, delay)
            raise task.retry(exc=exc, countdown=delay, max_retries=MAX_ATTEMPTS - 1) from exc
        log.error("%s: выгрузка %s не удалась, попыток %d", task.name, export_id, attempt, exc_info=exc)
        events.dead_letter(task=task.name, stage="export", args=list(task.request.args or []), error=repr(exc),
                           attempts=attempt)
        message = str(exc) if isinstance(exc, ExportError) else EXPORT_FAILED
        try:
            _finish(export_id, error=message)
        except Exception:
            log.exception("Выгрузка %s: не удалось записать ошибку", export_id)
        return None


def _export_row(export_id: str) -> tuple[str, str, bool]:
    with runtime.session_scope() as s:
        export = s.get(Export, as_uuid(export_id))
        if export is None:
            raise ExportError("Выгрузка не найдена")
        jsonlog.bind(**{"batch_id" if export.scope == "batch" else "document_id": str(export.target_id)})
        return export.scope, str(export.target_id), export.include_rejected


def render_document(document_id: str, key: str, *, include_rejected: bool = False):
    """Размеченный файл документа → MinIO под ключом `key`; возвращает `MarkResult` и формат файла."""
    from worker import markup

    with runtime.session_scope() as s:
        payload = exports.export_payload(s, document_id, include_rejected=include_rejected)
    doc, findings = payload["document"], payload["findings"]
    if doc["kind"] in ("doc", "rtf"):
        source, fmt = doc["normalized_key"], "docx"
    elif doc["kind"] == "docx":
        source, fmt = doc["object_key"], "docx"
    elif doc["kind"] in ("pdf", "scan"):
        source, fmt = doc["object_key"], "pdf"
    else:
        raise ExportError(f"Формат «{doc['kind']}» не размечается")
    if not source:
        raise ExportError("Документ ещё не обработан — выгрузка станет доступна после проверки")
    storage = runtime.get_storage()
    with tempfile.TemporaryDirectory(prefix="b2b-export-") as tmp:
        src, out = Path(tmp) / f"source.{fmt}", Path(tmp) / f"marked.{fmt}"
        storage.download(source, src)
        mark = markup.mark_docx if fmt == "docx" else markup.mark_pdf
        result = mark(src, findings, out)
        storage.put_file(key, out, content_type=MIME[fmt])
    log.info("Выгрузка: документ %s (%s): находок размечено %d, без места %d", document_id, doc["filename"],
             result.placed, len(result.skipped))
    return result, fmt


# --- Документ --------------------------------------------------------------------------------


@celery_app.task(name="worker.exports.export_document", bind=True)
def export_document(self, export_id: str) -> dict | None:
    """`POST /api/documents/{id}/exports`: размеченный DOCX или PDF (FR-7.1, FR-7.2)."""
    return _run_export(self, export_id, lambda: _export_document(export_id))


def _export_document(export_id: str) -> dict:
    _, document_id, include_rejected = _export_row(export_id)
    with runtime.session_scope() as s:
        doc = s.get(Document, as_uuid(document_id))
        fmt = "docx" if doc.kind in ("docx", "doc", "rtf") else "pdf"
    key = result_key(export_id, fmt)
    render_document(document_id, key, include_rejected=include_rejected)
    return _finish(export_id, key=key)


# --- Пакет -----------------------------------------------------------------------------------


@celery_app.task(name="worker.exports.export_batch", bind=True)
def export_batch(self, export_id: str):
    """`POST /api/batches/{id}/exports`: документы параллельно, затем zip со сводкой (FR-8.1)."""
    ids = _run_export(self, export_id, lambda: _done_documents(export_id))
    if ids is None:
        return None
    body = build_batch_zip.s(export_id)
    if not ids:
        return self.replace(build_batch_zip.si([], export_id))
    return self.replace(chord([export_batch_document.si(export_id, d) for d in ids], body))


def _done_documents(export_id: str) -> list[str]:
    _, batch_id, _ = _export_row(export_id)
    with runtime.session_scope() as s:
        return [str(d) for d in s.scalars(select(Document.id).where(
            Document.batch_id == as_uuid(batch_id), Document.status == "done").order_by(Document.original_path))]


@celery_app.task(name="worker.exports.export_batch_document", bind=True)
def export_batch_document(self, export_id: str, document_id: str) -> dict:
    """Один документ пакета; ошибка не роняет chord — документ попадёт в архив без разметки."""
    try:
        _, _, include_rejected = _export_row(export_id)
        with runtime.session_scope() as s:
            doc = s.get(Document, as_uuid(document_id))
            fmt = "docx" if doc.kind in ("docx", "doc", "rtf") else "pdf"
        key = part_key(export_id, document_id, fmt)
        render_document(document_id, key, include_rejected=include_rejected)
        return {"document_id": document_id, "key": key, "format": fmt}
    except Exception as exc:
        attempt = self.request.retries + 1
        if is_retryable(exc) and attempt < MAX_ATTEMPTS:
            raise self.retry(exc=exc, countdown=retry_delay(self.request.retries),
                             max_retries=MAX_ATTEMPTS - 1) from exc
        log.error("Выгрузка %s: документ %s не размечен", export_id, document_id, exc_info=exc)
        events.dead_letter(task=self.name, stage="export", document_id=document_id, args=[export_id, document_id],
                           error=repr(exc), attempts=attempt)
        return {"document_id": document_id, "key": None, "error": repr(exc)}


@celery_app.task(name="worker.exports.build_batch_zip", bind=True)
def build_batch_zip(self, results: list[dict] | None, export_id: str) -> dict | None:
    """Zip пакета: размеченные документы по путям исходного архива, в корне — `Сводка.xlsx` (FR-8.1–8.3)."""
    return _run_export(self, export_id, lambda: _build_batch_zip(export_id, results or []))


def archive_path(original_path: str, fmt: str | None, used: set[str]) -> str:
    """Путь документа в zip: как в исходном архиве, DOC и RTF — с расширением размеченного DOCX.

    Пути из БД уже безопасны (распаковка запрещает `..`), здесь они ещё раз нормализуются; одинаковые
    пути из двух загруженных архивов получают номер: «Договор (2).docx».
    """
    parts = [p for p in PurePosixPath(original_path.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    path = PurePosixPath(*parts) if parts else PurePosixPath("документ")
    if fmt and path.suffix.lower() != f".{fmt}":
        path = path.with_suffix(f".{fmt}")
    candidate, n = str(path), 2
    while candidate.lower() in used:
        candidate = str(path.with_name(f"{path.stem} ({n}){path.suffix}"))
        n += 1
    used.add(candidate.lower())
    return candidate


def _build_batch_zip(export_id: str, results: list[dict]) -> dict:
    from worker import markup

    _, batch_id, include_rejected = _export_row(export_id)
    marked = {r["document_id"]: r for r in results if r.get("key")}
    with runtime.session_scope() as s:
        summary = exports.batch_summary(s, batch_id, include_rejected=include_rejected)
        docs = [(str(d.id), d.original_path or d.filename, d.object_key)
                for d in s.scalars(select(Document).where(Document.batch_id == as_uuid(batch_id),
                                                          Document.status == "done")
                                   .order_by(Document.original_path))]
        batch = s.get(Batch, as_uuid(batch_id))
        log.info("Выгрузка пакета %s (%s): документов %d", batch_id, batch.name, len(docs))
    storage = runtime.get_storage()
    notes: list[str] = []
    key = result_key(export_id, "zip")
    used: set[str] = {SUMMARY_NAME.lower()}
    with tempfile.TemporaryDirectory(prefix="b2b-export-") as tmp:
        out = Path(tmp) / "batch.zip"
        local = Path(tmp) / "part"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for document_id, original_path, object_key in docs:
                part = marked.get(document_id)
                if part:
                    storage.download(part["key"], local)
                    zf.write(local, archive_path(original_path, part["format"], used))
                else:
                    # Разметка не удалась: в архив идёт исходный файл, в «Итогах» — примечание
                    storage.download(object_key, local)
                    zf.write(local, archive_path(original_path, None, used))
                    notes.append(f"{original_path}: {NOT_MARKED.lower()}")
            xlsx = Path(tmp) / "summary.xlsx"
            markup.summary_xlsx(summary, xlsx, notes=notes)
            zf.write(xlsx, SUMMARY_NAME)
        storage.put_file(key, out, content_type=MIME["zip"])
    data = _finish(export_id, key=key)
    # Части удаляются после записи итога: повтор упавшего `_finish` ещё найдёт их
    storage.delete(r["key"] for r in marked.values())
    return data
