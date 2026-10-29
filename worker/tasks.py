"""Задачи конвейера: от файлов пакета до блоков и фрагментов документа (FR-2.1, FR-3.3, FR-3.4).

Контракт 3 — сигнатуры задач. Все задачи принимают только id, результат пишут в БД
(промежуточные данные — JSON-файлами в MinIO рядом с исходником) и идемпотентны: повтор
перезаписывает результат этапа по ключу `(document_id, stage)` и не создаёт дублей.

    unpack_batch(batch_id)                  ingest    файлы пакета → документы; заменяет себя на chord документов
    extract_text(document_id)               ingest    DOCX сразу; DOC и RTF через LibreOffice; PDF — план страниц
    ocr_pages(document_id, pages, total)    ocr       OCR группы страниц скана (по 5), только слова
    build_pdf_result(document_id)           ingest    абзацы PDF из текстового слоя и всех групп OCR
    build_structure(document_id)            ingest    блоки с разделами → таблица block
    index_chunks(document_id)               analysis  фрагменты с эмбеддингами → таблица chunk
    analyze_document(document_id)           analysis  находки ИИ → merge_findings (анализ по правилам — этап 5)
    finalize_batch(batch_id)                ingest    статус и счётчики пакета

Канва пакета: `unpack_batch` → `chord(group(цепочка документа, ...), finalize_batch)`, цепочка документа —
`extract_text | build_structure | index_chunks | analyze_document`. У скана `extract_text` заменяет себя на
`chord(group(ocr_pages по 5 страниц), build_pdf_result)`, остаток цепочки продолжается после него.

Ошибка документа не останавливает пакет: задача пишет `fail_document` и завершается без исключения,
следующие этапы этого документа видят статус `error` и пропускают его, chord пакета доходит до
`finalize_batch`. Промежуточные файлы документа: `batches/{batch}/work/{document}/` — `plan.json`,
`ocr/0001-0005.json`, `extraction.json`, `converted.docx` (DOC и RTF; его же размечает экспорт).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from celery import chain, chord
from celery.canvas import Signature

import ml
from contracts.extraction import ExtractionResult, OcrPage, PageInfo
from db import batches, pipeline
from db.models import Document
from ml.errors import MLError, UnsupportedFormatError
from worker import runtime
from worker.app import celery_app
from worker.archive import Limits, Unpacker, extension
from worker.progress import ocr_page_done, report_progress

log = logging.getLogger(__name__)

# Страницы скана идут в очередь ocr группами: группы распознаются параллельно (FR-3.3)
OCR_PAGES_PER_TASK = int(os.environ.get("OCR_PAGES_PER_TASK") or 5)
# id документа выводится из файла и пути в архиве: повтор распаковки находит те же документы
DOCUMENT_NAMESPACE = uuid.UUID("6f1c2a52-8d0e-4c1b-9a43-2b7f0e5d9c11")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CORRUPTED_MESSAGE = "Файл повреждён и не открывается — он не будет проверен"
INTERNAL_ERROR = ("internal_error", "Не удалось обработать документ из-за внутренней ошибки — попробуйте повторить")

# Процент документа на границах этапов: извлечение до 45, OCR — от 10 до 60, анализ — с 65
OCR_START, OCR_SPAN = 10, 50


@dataclass(frozen=True)
class DocInfo:
    id: str
    batch_id: str
    filename: str
    kind: str
    object_key: str


# --- Обвязка этапа документа -----------------------------------------------------------------


def _run_stage(document_id: str, stage: str, work: Callable[[DocInfo], Any]) -> Any:
    """Журнал `task_run`, пропуск упавшего документа, ошибка → `fail_document` без исключения наружу."""
    with runtime.session_scope() as s:
        doc = s.get(Document, pipeline.as_uuid(document_id))
        if doc is None:
            log.warning("%s: документ %s не найден — пропуск", stage, document_id)
            return None
        if doc.status == "error":
            log.info("%s: документ %s уже с ошибкой — этап пропущен", stage, document_id)
            return None
        info = DocInfo(id=str(doc.id), batch_id=str(doc.batch_id), filename=doc.filename, kind=doc.kind,
                       object_key=doc.object_key)
        run_id = pipeline.start_task_run(s, batch_id=doc.batch_id, stage=stage, document_id=doc.id).id
        batches.mark_processing(s, doc.batch_id)
    try:
        result = work(info)
    except MLError as exc:
        log.warning("%s: документ %s (%s): %s", stage, info.id, info.filename, exc)
        _fail(info, run_id, exc.code, exc.message, detail=str(exc))
        return None
    except Exception as exc:
        log.exception("%s: документ %s (%s): внутренняя ошибка", stage, info.id, info.filename)
        _fail(info, run_id, *INTERNAL_ERROR, detail=repr(exc))
        return None
    with runtime.session_scope() as s:
        pipeline.finish_task_run(s, run_id)
    return result


def _fail(info: DocInfo, run_id, code: str, message: str, *, detail: str) -> None:
    with runtime.session_scope() as s:
        pipeline.finish_task_run(s, run_id, error=f"{code}: {detail}"[:2000])
        pipeline.fail_document(s, info.id, code, message)


def _download(info: DocInfo, workdir: Path) -> Path:
    suffix = PurePosixPath(info.object_key).suffix or f".{info.kind}"
    path = workdir / f"source{suffix.lower()}"
    runtime.get_storage().download(info.object_key, path)
    return path


def _put_json(info: DocInfo, name: str, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode()
    runtime.get_storage().put_bytes(runtime.work_key(info.batch_id, info.id, name), payload,
                                    content_type="application/json")


def _get_json(info: DocInfo, name: str) -> Any:
    return json.loads(runtime.get_storage().get_bytes(runtime.work_key(info.batch_id, info.id, name)))


def ocr_groups(pages: list[int], size: int | None = None) -> list[list[int]]:
    size = size or OCR_PAGES_PER_TASK
    return [pages[i:i + size] for i in range(0, len(pages), size)]


def _ocr_name(pages: list[int]) -> str:
    return f"ocr/{pages[0]:04d}-{pages[-1]:04d}.json"


def _save_extraction(info: DocInfo, result: ExtractionResult, *, kind: str | None = None,
                     normalized_key: str | None = None) -> None:
    """Результат извлечения — в MinIO (вход `build_structure`), страницы и вид документа — в БД."""
    runtime.get_storage().put_bytes(runtime.work_key(info.batch_id, info.id, "extraction.json"),
                                    result.model_dump_json().encode(), content_type="application/json")
    with runtime.session_scope() as s:
        doc = s.get(Document, pipeline.as_uuid(info.id))
        if kind and kind != doc.kind:
            doc.kind = kind  # расширение файла не совпало с содержимым: вид — по сигнатуре
        if normalized_key:
            doc.normalized_key = normalized_key
        pipeline.save_extraction_info(s, info.id, result)
        pipeline.set_stage(s, info.id, "extracting", progress=45)


# --- Пакет -----------------------------------------------------------------------------------


def document_chain(document_id: str) -> Signature:
    d = str(document_id)
    return chain(extract_text.si(d), build_structure.si(d), index_chunks.si(d), analyze_document.si(d))


def _documents_chord(batch_id: str, document_ids: list[str], make: Callable[[str], Signature]) -> Signature:
    if not document_ids:
        return finalize_batch.si(batch_id)
    return chord([make(d) for d in document_ids], finalize_batch.si(batch_id))


def process_batch(batch_id: str) -> None:
    """Пакет запущен (`POST /batches/{id}/start`): поставить конвейер в очередь."""
    unpack_batch.apply_async((str(batch_id),))


def rerun_batch(batch_id: str, document_ids: list[str]) -> None:
    """Повторный анализ (`POST /batches/{id}/rerun`): текст и фрагменты уже есть, заново — только анализ."""
    _documents_chord(str(batch_id), [str(d) for d in document_ids], analyze_document.si).apply_async()


@celery_app.task(name="worker.tasks.unpack_batch", bind=True)
def unpack_batch(self, batch_id: str):
    """Файлы пакета → документы, затем chord по документам; распаковка zip — с защитой от zip-бомбы."""
    document_ids = unpack(batch_id)
    log.info("unpack_batch: пакет %s: документов в работу %d", batch_id, len(document_ids))
    return self.replace(_documents_chord(batch_id, document_ids, document_chain))


def unpack(batch_id: str) -> list[str]:
    """Создаёт документы пакета и возвращает id тех, что идут в обработку (без ошибки на входе)."""
    with runtime.session_scope() as s:
        files = batches.checkable_files(s, batch_id)
        batches.mark_processing(s, batch_id)
        run_id = pipeline.start_task_run(s, batch_id=batch_id, stage="unpack_batch").id
    storage = runtime.get_storage()
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="b2b-unpack-") as tmp:
        workdir = Path(tmp)
        unpacker = Unpacker(workdir, on_file=lambda *_: None, limits=Limits.from_env())
        for f in files:
            if f["check"] in ("unsupported", "corrupted"):
                continue
            if f["archive_entries"] is None:
                failure = ("encrypted", f["message"]) if f["check"] == "encrypted" else None
                records.append(_record(f, path=f["filename"], kind=extension(f["filename"]),
                                       size=f["size_bytes"], object_key=f["object_key"], failure=failure))
                continue
            archive = workdir / "archive.zip"
            storage.download(f["object_key"], archive)
            seen = len(unpacker.problems)
            unpacker.on_file = lambda path, local, size, f=f: records.append(
                _store_entry(storage, batch_id, f, path, local, size))
            unpacker.unpack(archive, label=f["filename"])
            archive.unlink(missing_ok=True)
            for problem in unpacker.problems[seen:]:
                records.append(_record(f, path=problem.path, kind="zip", size=f["size_bytes"],
                                       object_key=f["object_key"], failure=(problem.code, problem.message)))

    queued: list[str] = []
    with runtime.session_scope() as s:
        for r in records:
            doc = s.get(Document, r["id"])
            if doc is None:
                doc = pipeline.create_document(
                    s, batch_id=batch_id, uploaded_file_id=r["uploaded_file_id"], filename=r["filename"],
                    original_path=r["original_path"], kind=r["kind"], size_bytes=r["size_bytes"],
                    object_key=r["object_key"], document_id=r["id"])
                if r["failure"]:
                    code, message = r["failure"]
                    pipeline.fail_document(s, doc.id, code, message or "Файл не будет проверен")
            if doc.status != "error":
                queued.append(str(doc.id))
        pipeline.finish_task_run(s, run_id)
    return queued


def _record(f: dict, *, path: str, kind: str, size: int, object_key: str,
            failure: tuple[str, str | None] | None, document_id: uuid.UUID | None = None) -> dict:
    return {
        "id": document_id or uuid.uuid5(DOCUMENT_NAMESPACE, f"{f['id']}:{path}"),
        "uploaded_file_id": f["id"], "filename": PurePosixPath(path).name, "original_path": path,
        "kind": kind, "size_bytes": size, "object_key": object_key, "failure": failure,
    }


def _store_entry(storage, batch_id: str, f: dict, path: str, local: Path, size: int) -> dict:
    """Файл из архива: проверка по содержимому и запись в MinIO под ключом из id документа."""
    document_id = uuid.uuid5(DOCUMENT_NAMESPACE, f"{f['id']}:{path}")
    check = ml.check_file(local)
    ext = extension(path)
    kind = check.kind if check.kind in ("pdf", "docx", "doc", "rtf") else ext
    key = runtime.unpacked_key(batch_id, str(document_id), ext)
    storage.put_file(key, local)
    failure = None if check.check == "ok" else (check.check, check.message)
    if check.check == "unsupported":
        # Расширение допустимое, а содержимое не похоже ни на один формат — как при загрузке файла
        failure = ("corrupted", CORRUPTED_MESSAGE)
    return _record(f, path=path, kind=kind, size=size, object_key=key, failure=failure, document_id=document_id)


@celery_app.task(name="worker.tasks.finalize_batch")
def finalize_batch(batch_id: str) -> dict:
    """Все документы дошли до `done` или `error`: статус пакета и счётчики (данные события `batch.done`)."""
    with runtime.session_scope() as s:
        run_id = pipeline.start_task_run(s, batch_id=batch_id, stage="finalize_batch").id
        result = pipeline.finish_batch(s, batch_id)
        pipeline.finish_task_run(s, run_id)
    log.info("finalize_batch: пакет %s: %s", batch_id, result.get("status"))
    return result


# --- Документ --------------------------------------------------------------------------------


@celery_app.task(name="worker.tasks.extract_text", bind=True)
def extract_text(self, document_id: str):
    """Текст документа; у PDF со сканами задача заменяет себя на chord групп OCR."""
    replacement = _run_stage(document_id, "extract_text", _extract_text)
    if replacement is not None:
        return self.replace(replacement)
    return None


def _extract_text(info: DocInfo) -> Signature | None:
    from ml.files import require_ok

    report_progress(info.id, "extracting", 5)
    storage = runtime.get_storage()
    with tempfile.TemporaryDirectory(prefix="b2b-extract-") as tmp:
        src = _download(info, Path(tmp))
        check = require_ok(src)
        if check.kind == "pdf":
            plan = ml.plan_pdf(src)
            _put_json(info, "plan.json", [p.model_dump(mode="json") for p in plan])
            need = [p.page for p in plan if p.needs_ocr]
            if need:
                groups = ocr_groups(need)
                log.info("extract_text: %s: %d стр. в OCR, групп %d", info.id, len(need), len(groups))
                report_progress(info.id, "ocr", OCR_START, {"page": 0, "pages": len(need)})
                return chord([ocr_pages.si(info.id, g, len(need)) for g in groups], build_pdf_result.si(info.id))
            _save_extraction(info, ml.build_pdf_result(src, plan, []), kind="pdf")
        elif check.kind == "docx":
            _save_extraction(info, ml.extract_docx(src), kind="docx")
        elif check.kind in ("doc", "rtf"):
            converted = ml.convert_to_docx(src)
            key = runtime.work_key(info.batch_id, info.id, "converted.docx")
            storage.put_file(key, converted, content_type=DOCX_MIME)
            pages = ml.doc_page_count(src) if check.kind == "doc" else None
            result = ml.extract_docx(converted, kind=check.kind, source_pages=pages)
            _save_extraction(info, result, kind=check.kind, normalized_key=key)
        else:
            raise UnsupportedFormatError()
    return None


@celery_app.task(name="worker.tasks.ocr_pages")
def ocr_pages(document_id: str, pages: list[int], total: int) -> None:
    """OCR группы страниц; слова — в `ocr/{первая}-{последняя}.json`, абзацы соберёт `build_pdf_result`."""
    _run_stage(document_id, "ocr_pages", lambda info: _ocr_group(info, pages, total))


def _ocr_group(info: DocInfo, pages: list[int], total: int) -> None:
    last = 0

    def progress(done: int, _count: int) -> None:
        nonlocal last
        if done > last:
            ocr_page_done(info.id, total, done - last, start=OCR_START, span=OCR_SPAN)
            last = done

    with tempfile.TemporaryDirectory(prefix="b2b-ocr-") as tmp:
        src = _download(info, Path(tmp))
        result = ml.ocr_pages(src, pages, progress=progress)
    _put_json(info, _ocr_name(pages), [p.model_dump(mode="json") for p in result])


@celery_app.task(name="worker.tasks.build_pdf_result")
def build_pdf_result(document_id: str) -> None:
    """Все группы OCR готовы: абзацы собираются один раз, иначе абзац на границе групп разорвётся."""
    _run_stage(document_id, "build_pdf_result", _build_pdf_result)


def _build_pdf_result(info: DocInfo) -> None:
    plan = [PageInfo.model_validate(p) for p in _get_json(info, "plan.json")]
    ocr: list[OcrPage] = []
    for group in ocr_groups([p.page for p in plan if p.needs_ocr]):
        ocr.extend(OcrPage.model_validate(p) for p in _get_json(info, _ocr_name(group)))
    with tempfile.TemporaryDirectory(prefix="b2b-pdf-") as tmp:
        src = _download(info, Path(tmp))
        result = ml.build_pdf_result(src, plan, ocr)
    _save_extraction(info, result)


@celery_app.task(name="worker.tasks.build_structure")
def build_structure(document_id: str) -> None:
    """Блоки с разделами (FR-3.1, FR-4.2); id блоков детерминированы — ручные пометки остаются на месте."""
    _run_stage(document_id, "build_structure", _build_structure)


def _build_structure(info: DocInfo) -> None:
    raw = runtime.get_storage().get_bytes(runtime.work_key(info.batch_id, info.id, "extraction.json"))
    blocks = ml.build_structure(ExtractionResult.model_validate_json(raw), document_id=info.id)
    with runtime.session_scope() as s:
        pipeline.save_blocks(s, info.id, blocks)
        pipeline.set_stage(s, info.id, "extracting", progress=60)


@celery_app.task(name="worker.tasks.index_chunks")
def index_chunks(document_id: str) -> None:
    """Фрагменты для поиска с эмбеддингами; старые фрагменты документа заменяются."""
    _run_stage(document_id, "index_chunks", _index_chunks)


def _index_chunks(info: DocInfo) -> None:
    report_progress(info.id, "analysis", 65)
    with runtime.session_scope() as s:
        blocks = pipeline.load_blocks(s, info.id)
    chunks = ml.index_chunks(blocks, document_id=info.id)
    with runtime.session_scope() as s:
        pipeline.save_chunks(s, info.id, chunks)
        pipeline.set_stage(s, info.id, "analysis", progress=80)


@celery_app.task(name="worker.tasks.analyze_document")
def analyze_document(document_id: str) -> None:
    """Находки ИИ документа и итог документа (`done`, цвет, счётчики).

    Анализ по правилам подключает этап 5: `batch_rules` → `group_rules` → chord из `analyze_rule_group`
    → `merge_findings`. Пока результат пустой: документ завершается без находок ИИ, ручные остаются.
    """
    _run_stage(document_id, "analyze_document", _analyze_document)


def _analyze_document(info: DocInfo) -> None:
    with runtime.session_scope() as s:
        pipeline.merge_findings(s, info.id, [])
