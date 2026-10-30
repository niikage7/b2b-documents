"""Задачи конвейера: от файлов пакета до находок (FR-2.1, FR-3.3, FR-3.4, FR-4.1, FR-9.3).

Контракт 3 — сигнатуры задач. Все задачи принимают только id, результат пишут в БД
(промежуточные данные — JSON-файлами в MinIO рядом с исходником) и идемпотентны: повтор
перезаписывает результат этапа по ключу `(document_id, stage)` и не создаёт дублей.

    unpack_batch(batch_id)                  ingest    файлы пакета → документы; заменяет себя на chord документов
    extract_text(document_id)               ingest    DOCX сразу; DOC и RTF через LibreOffice; PDF — план страниц
    ocr_pages(document_id, pages, total)    ocr       OCR группы страниц скана (по 5), только слова
    build_pdf_result(document_id)           ingest    абзацы PDF из текстового слоя и всех групп OCR
    build_structure(document_id)            ingest    блоки с разделами → таблица block
    index_chunks(document_id)               analysis  фрагменты с эмбеддингами → таблица chunk
    extract_batch_meta(batch_id, doc_ids)   analysis  предмет закупки → batch.subject; заменяет себя на chord анализа
    analyze_document(document_id)           analysis  правила пакета → группы по 3–4; заменяет себя на chord групп
    analyze_rule_group(document_id, group)  analysis  кандидаты в находки одной группы → analysis/group-NN.json
    merge_findings(document_id)             analysis  находки ИИ всех групп → БД; документ done, цвет и счётчики
    extract_passport(batch_id, doc_ids)     analysis  паспорт закупки → batch.meta, параллельно с анализом
    finalize_batch(batch_id)                ingest    статус и счётчики пакета
    rule_test(payload)                      analysis  тест правила с экрана «Правила», без записи в БД

Канва пакета:

    unpack_batch
      → chord(group(extract_text | build_structure | index_chunks, ...), extract_batch_meta)
      → chord(group(analyze_document, ..., extract_passport), finalize_batch)
    analyze_document → chord(group(analyze_rule_group по группам правил), merge_findings)

Анализ ждёт текст всех документов пакета: предмет закупки входит в запрос каждого правила, а берётся он
из извещения. У скана `extract_text` заменяет себя на `chord(group(ocr_pages по 5 страниц),
build_pdf_result)`, остаток цепочки продолжается после него. Повторный анализ (`rerun_batch`) начинается
с `extract_batch_meta`: предмет и паспорт уже есть, заново считаются только находки.

`rule_test` — исключение из правила «только id»: правило может быть ещё не сохранено, поэтому задача
получает его целиком вместе с текстом или id документа и возвращает результат, а не пишет его в БД.

Ошибка документа не останавливает пакет: задача пишет `fail_document` и завершается без исключения,
следующие этапы этого документа видят статус `error` и пропускают его, chord пакета доходит до
`finalize_batch`; задачи пакета (`extract_batch_meta`, `extract_passport`) при сбое пишут журнал и идут
дальше без предмета или паспорта.

Надёжность (FR-2.3): временная ошибка этапа (ошибка ML с `retryable`, обрыв связи с БД, MinIO или Redis)
повторяется с экспоненциальной паузой — `TASK_RETRY_BASE_SECONDS` × 2^n, всего `TASK_MAX_ATTEMPTS`
попыток (3). Повтор Celery сохраняет место задачи в chain и chord. После последней неудачи документ
получает «Ошибка», а запись уходит в dead-letter (`worker.events.dead_letter`); туда же попадают
внутренние ошибки и задачи, которые упали с исключением (сигнал `task_failure`). Упавший документ
перезапускается с этапа, на котором упал: `retry_document` (`POST /api/documents/{id}/retry`).

События для SSE (FR-2.2) публикует `worker.progress` после фиксации транзакции этапа. Промежуточные файлы документа: `batches/{batch}/work/{document}/` —
`plan.json`, `ocr/0001-0005.json`, `extraction.json`, `converted.docx` (DOC и RTF; его же размечает
экспорт), `analysis/plan.json` (коды правил по группам), `analysis/group-01.json` (`RuleGroupResult`).
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from celery import chain, chord
from celery.canvas import Signature
from celery.signals import task_failure

import ml
from contracts.analysis import RuleGroupResult, RuleSpec
from contracts.extraction import ExtractionResult, OcrPage, PageInfo
from db import batches, pipeline
from db.models import Batch, Document
from ml.errors import CorruptedFileError, MLError, UnsupportedFormatError
from worker import events, jsonlog, progress, runtime
from worker.app import celery_app
from worker.archive import Limits, Unpacker, extension
from worker.progress import ocr_page_done, report_progress, rules_done

log = logging.getLogger(__name__)

# Страницы скана идут в очередь ocr группами: группы распознаются параллельно (FR-3.3)
OCR_PAGES_PER_TASK = int(os.environ.get("OCR_PAGES_PER_TASK") or 5)
# id документа выводится из файла и пути в архиве: повтор распаковки находит те же документы
DOCUMENT_NAMESPACE = uuid.UUID("6f1c2a52-8d0e-4c1b-9a43-2b7f0e5d9c11")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CORRUPTED_MESSAGE = "Файл повреждён и не открывается — он не будет проверен"
INTERNAL_ERROR = ("internal_error", "Не удалось обработать документ из-за внутренней ошибки — попробуйте повторить")

# Процент документа на границах этапов: извлечение до 45, OCR — от 10 до 60, фрагменты — 65–80,
# анализ по правилам — от 80 до 98
OCR_START, OCR_SPAN = 10, 50
ANALYSIS_START, ANALYSIS_SPAN = 80, 18

# Повторы временных ошибок: всего попыток и база экспоненциальной паузы (5, 10, 20 с …)
MAX_ATTEMPTS = max(1, int(os.environ.get("TASK_MAX_ATTEMPTS") or 3))
RETRY_BASE_SECONDS = float(os.environ.get("TASK_RETRY_BASE_SECONDS") or 5)
RETRY_MAX_SECONDS = 300


def _transient_errors() -> tuple[type[BaseException], ...]:
    """Сбои окружения, которые проходят сами: обрыв связи с PostgreSQL, MinIO, Redis, таймауты."""
    from sqlalchemy.exc import InterfaceError, OperationalError

    out: list[type[BaseException]] = [ConnectionError, TimeoutError, OperationalError, InterfaceError]
    try:
        from urllib3.exceptions import HTTPError

        out.append(HTTPError)
    except ImportError:  # pragma: no cover — urllib3 приходит с клиентом MinIO
        pass
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        out += [RedisConnectionError, RedisTimeoutError]
    except ImportError:  # pragma: no cover
        pass
    return tuple(out)


TRANSIENT_ERRORS = _transient_errors()


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, MLError):
        return bool(exc.retryable)
    return isinstance(exc, TRANSIENT_ERRORS)


def retry_delay(retries: int) -> float:
    """Пауза перед повтором номер `retries + 1`: экспонента с разбросом ±20 %, чтобы повторы не шли волной."""
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** retries) * random.uniform(0.8, 1.2)


@dataclass(frozen=True)
class DocInfo:
    id: str
    batch_id: str
    filename: str
    kind: str
    object_key: str


# --- Обвязка этапа документа -----------------------------------------------------------------


def _run_stage(task, document_id: str, stage: str, work: Callable[[DocInfo], Any]) -> Any:
    """Журнал `task_run`, пропуск упавшего документа, повтор временной ошибки с паузой;
    последняя неудача → `fail_document` и dead-letter без исключения наружу (chord пакета не ломается).
    """
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
        jsonlog.bind(batch_id=info.batch_id, document_id=info.id)
        run_id = pipeline.start_task_run(s, batch_id=doc.batch_id, stage=stage, document_id=doc.id).id
        batches.mark_processing(s, doc.batch_id)
    try:
        result = work(info)
    except Exception as exc:
        retryable = is_retryable(exc)
        attempt = task.request.retries + 1
        code, message = (exc.code, exc.message) if isinstance(exc, MLError) else INTERNAL_ERROR
        detail = str(exc) if isinstance(exc, MLError) else repr(exc)
        if retryable and attempt < MAX_ATTEMPTS:
            delay = retry_delay(task.request.retries)
            log.warning("%s: документ %s (%s): попытка %d из %d не удалась (%s), повтор через %.0f с",
                        stage, info.id, info.filename, attempt, MAX_ATTEMPTS, detail, delay)
            with runtime.session_scope() as s:
                pipeline.finish_task_run(s, run_id, error=f"{code}: {detail}"[:2000])
            raise task.retry(exc=exc, countdown=delay, max_retries=MAX_ATTEMPTS - 1) from exc
        if isinstance(exc, MLError) and not retryable:
            log.warning("%s: документ %s (%s): %s", stage, info.id, info.filename, exc)
        else:
            log.error("%s: документ %s (%s): ошибка, попыток %d", stage, info.id, info.filename, attempt,
                      exc_info=exc)
            events.dead_letter(task=task.name, stage=stage, batch_id=info.batch_id, document_id=info.id,
                               args=list(task.request.args or []), error=f"{code}: {detail}", attempts=attempt)
        _fail(info, run_id, code, message, detail=detail, retryable=retryable or not isinstance(exc, MLError))
        return None
    with runtime.session_scope() as s:
        pipeline.finish_task_run(s, run_id)
    return result


def _fail(info: DocInfo, run_id, code: str, message: str, *, detail: str, retryable: bool) -> None:
    with runtime.session_scope() as s:
        pipeline.finish_task_run(s, run_id, error=f"{code}: {detail}"[:2000])
        failed = pipeline.fail_document(s, info.id, code, message)
    progress.publish_document_error(info.batch_id, failed, retryable=retryable)


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
        data = pipeline.set_stage(s, info.id, "extracting", progress=45)
    progress.publish_progress(info.batch_id, data)


# --- Пакет -----------------------------------------------------------------------------------


def document_chain(document_id: str) -> Signature:
    """Текст документа: извлечение, структура, фрагменты. Анализ начинается после всех документов пакета."""
    d = str(document_id)
    return chain(extract_text.si(d), build_structure.si(d), index_chunks.si(d))


def process_batch(batch_id: str) -> None:
    """Пакет запущен (`POST /batches/{id}/start`): поставить конвейер в очередь."""
    unpack_batch.apply_async((str(batch_id),))


def rerun_batch(batch_id: str, document_ids: list[str]) -> None:
    """Повторный анализ (`POST /batches/{id}/rerun`): текст и фрагменты уже есть, заново — только анализ."""
    extract_batch_meta.apply_async((str(batch_id), [str(d) for d in document_ids]))


# Этап журнала `task_run`, на котором упал документ → с какой задачи цепочки текста начинать повтор.
# Этапы анализа перезапускаются с `analyze_document`; неизвестный этап — с начала
TEXT_RESTART = {"extract_text": 0, "ocr_pages": 0, "build_pdf_result": 0, "extracting": 0,
                "build_structure": 1, "index_chunks": 2}
ANALYSIS_STAGES = {"analyze_document", "analyze_rule_group", "merge_findings"}


def restart_stage(s, document_id) -> str:
    """Этап, с которого повторять документ: последний упавший этап по журналу `task_run`.

    `pipeline.failed_stage` смотрит на последнюю запись документа, а при параллельных группах OCR и
    правил последней может закончиться успешная группа; здесь — последняя запись с ошибкой.
    Записи нет (файл не прошёл проверку на входе) — с начала.
    """
    from sqlalchemy import select

    from db.models import TaskRun

    stage = s.scalar(select(TaskRun.stage)
                     .where(TaskRun.document_id == pipeline.as_uuid(document_id), TaskRun.status == "error")
                     .order_by(TaskRun.started_at.desc(), TaskRun.attempt.desc()).limit(1))
    return stage or "extracting"


def retry_signature(batch_id: str, document_id: str, stage: str) -> Signature:
    """Цепочка повтора документа с упавшего этапа (`restart_stage`).

    Заканчивается `extract_batch_meta`, как у пакета: предмет и паспорт, если их ещё нет (например,
    упало само извещение), затем анализ документа и `finalize_batch`.
    """
    b, d = str(batch_id), str(document_id)
    tail = extract_batch_meta.si(b, [d])
    if stage in ANALYSIS_STAGES:
        return tail
    steps = (extract_text, build_structure, index_chunks)
    return chain(*[t.si(d) for t in steps[TEXT_RESTART.get(stage, 0):]], tail)


def retry_document(batch_id: str, document_id: str, stage: str) -> None:
    """`POST /api/documents/{id}/retry`: документ снова в работе с этапа, на котором упал."""
    retry_signature(batch_id, document_id, stage).apply_async()
    log.info("retry_document: документ %s перезапущен с этапа %s", document_id, stage)


# Задачи пакета не ловят ошибки сами: временный сбой повторяется, остальное — в dead-letter (task_failure)
_BATCH_RETRY = {"autoretry_for": TRANSIENT_ERRORS, "max_retries": MAX_ATTEMPTS - 1,
                "retry_backoff": RETRY_BASE_SECONDS, "retry_backoff_max": RETRY_MAX_SECONDS, "retry_jitter": True}


@celery_app.task(name="worker.tasks.unpack_batch", bind=True, **_BATCH_RETRY)
def unpack_batch(self, batch_id: str):
    """Файлы пакета → документы, затем chord по документам; распаковка zip — с защитой от zip-бомбы."""
    document_ids = unpack(batch_id)
    log.info("unpack_batch: пакет %s: документов в работу %d", batch_id, len(document_ids))
    progress.publish_snapshot(batch_id)  # документы появились: интерфейс показывает их карточки
    if not document_ids:
        return self.replace(finalize_batch.si(batch_id))
    return self.replace(chord([document_chain(d) for d in document_ids],
                              extract_batch_meta.si(batch_id, document_ids)))


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


@celery_app.task(name="worker.tasks.finalize_batch", **_BATCH_RETRY)
def finalize_batch(batch_id: str) -> dict:
    """Все документы дошли до `done` или `error`: статус пакета и счётчики, событие `batch.done`."""
    with runtime.session_scope() as s:
        run_id = pipeline.start_task_run(s, batch_id=batch_id, stage="finalize_batch").id
        result = pipeline.finish_batch(s, batch_id)
        pipeline.finish_task_run(s, run_id)
    log.info("finalize_batch: пакет %s: %s", batch_id, result.get("status"))
    progress.publish_batch_finished(batch_id, result)
    return result


@task_failure.connect
def _task_failed(sender=None, task_id=None, exception=None, args=None, **_):
    """Задача упала с исключением после всех повторов (этапы документа так не падают — см. `_run_stage`)."""
    request = getattr(sender, "request", None)
    events.dead_letter(task=getattr(sender, "name", str(sender)), stage=None, args=list(args or []),
                       error=repr(exception), attempts=(getattr(request, "retries", 0) or 0) + 1)


# --- Документ --------------------------------------------------------------------------------


@celery_app.task(name="worker.tasks.extract_text", bind=True)
def extract_text(self, document_id: str):
    """Текст документа; у PDF со сканами задача заменяет себя на chord групп OCR."""
    replacement = _run_stage(self, document_id, "extract_text", _extract_text)
    if replacement is not None:
        return self.replace(replacement)
    return None


def _extract_text(info: DocInfo) -> Signature | None:
    from ml.files import require_ok

    report_progress(info.id, "extracting", 5)
    storage = runtime.get_storage()
    with tempfile.TemporaryDirectory(prefix="b2b-extract-") as tmp:
        src = _download(info, Path(tmp))
        try:
            check = require_ok(src)
        except UnsupportedFormatError:
            # Документы создаются только для допустимых расширений: непохожее содержимое — повреждение,
            # как при распаковке; повтор документа показывает ту же причину
            raise CorruptedFileError(CORRUPTED_MESSAGE) from None
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


@celery_app.task(name="worker.tasks.ocr_pages", bind=True)
def ocr_pages(self, document_id: str, pages: list[int], total: int) -> None:
    """OCR группы страниц; слова — в `ocr/{первая}-{последняя}.json`, абзацы соберёт `build_pdf_result`."""
    _run_stage(self, document_id, "ocr_pages", lambda info: _ocr_group(info, pages, total))


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


@celery_app.task(name="worker.tasks.build_pdf_result", bind=True)
def build_pdf_result(self, document_id: str) -> None:
    """Все группы OCR готовы: абзацы собираются один раз, иначе абзац на границе групп разорвётся."""
    _run_stage(self, document_id, "build_pdf_result", _build_pdf_result)


def _build_pdf_result(info: DocInfo) -> None:
    plan = [PageInfo.model_validate(p) for p in _get_json(info, "plan.json")]
    ocr: list[OcrPage] = []
    for group in ocr_groups([p.page for p in plan if p.needs_ocr]):
        ocr.extend(OcrPage.model_validate(p) for p in _get_json(info, _ocr_name(group)))
    with tempfile.TemporaryDirectory(prefix="b2b-pdf-") as tmp:
        src = _download(info, Path(tmp))
        result = ml.build_pdf_result(src, plan, ocr)
    _save_extraction(info, result)


@celery_app.task(name="worker.tasks.build_structure", bind=True)
def build_structure(self, document_id: str) -> None:
    """Блоки с разделами (FR-3.1, FR-4.2); id блоков детерминированы — ручные пометки остаются на месте."""
    _run_stage(self, document_id, "build_structure", _build_structure)


def _build_structure(info: DocInfo) -> None:
    raw = runtime.get_storage().get_bytes(runtime.work_key(info.batch_id, info.id, "extraction.json"))
    blocks = ml.build_structure(ExtractionResult.model_validate_json(raw), document_id=info.id)
    with runtime.session_scope() as s:
        pipeline.save_blocks(s, info.id, blocks)
        data = pipeline.set_stage(s, info.id, "extracting", progress=60)
    progress.publish_progress(info.batch_id, data)


@celery_app.task(name="worker.tasks.index_chunks", bind=True)
def index_chunks(self, document_id: str) -> None:
    """Фрагменты для поиска с эмбеддингами; старые фрагменты документа заменяются."""
    _run_stage(self, document_id, "index_chunks", _index_chunks)


def _index_chunks(info: DocInfo) -> None:
    report_progress(info.id, "analysis", 65)
    with runtime.session_scope() as s:
        blocks = pipeline.load_blocks(s, info.id)
    chunks = ml.index_chunks(blocks, document_id=info.id)
    with runtime.session_scope() as s:
        pipeline.save_chunks(s, info.id, chunks)
        data = pipeline.set_stage(s, info.id, "analysis", progress=80)
    progress.publish_progress(info.batch_id, data)


# --- Анализ ----------------------------------------------------------------------------------


def _batch_stage(batch_id: str, stage: str, work: Callable[[], Any]) -> Any:
    """Этап пакета без права упасть: сбой пишется в журнал, конвейер идёт дальше без его результата."""
    with runtime.session_scope() as s:
        run_id = pipeline.start_task_run(s, batch_id=batch_id, stage=stage).id
    try:
        result = work()
    except Exception as exc:
        log.exception("%s: пакет %s: сбой, этап пропущен", stage, batch_id)
        with runtime.session_scope() as s:
            pipeline.finish_task_run(s, run_id, error=repr(exc)[:2000])
        events.dead_letter(task=f"worker.tasks.{stage}", stage=stage, batch_id=batch_id, error=repr(exc), attempts=1)
        return None
    with runtime.session_scope() as s:
        pipeline.finish_task_run(s, run_id)
    return result


def _ready_documents(document_ids: list[str]) -> list[tuple[str, str, list]]:
    """Документы с текстом (без ошибки): [(id, имя файла, блоки)] — вход предмета и паспорта закупки."""
    out = []
    with runtime.session_scope() as s:
        for d in document_ids:
            doc = s.get(Document, pipeline.as_uuid(d))
            if doc is not None and doc.status != "error":
                out.append((str(doc.id), doc.filename, pipeline.load_blocks(s, doc.id)))
    return out


def _search(document_ids: list[str]):
    """Фрагменты документов из таблицы `chunk` в памяти: транзакция не держится, пока идут вызовы LLM."""
    from ml.analysis.search import InMemorySearch

    with runtime.session_scope() as s:
        pg = ml.PgVectorSearch(s.connection().connection.driver_connection)
        return InMemorySearch({d: pg.chunks(d) for d in document_ids})


@celery_app.task(name="worker.tasks.extract_batch_meta", bind=True)
def extract_batch_meta(self, batch_id: str, document_ids: list[str]):
    """Предмет закупки, затем анализ документов и паспорт закупки параллельно → `finalize_batch`."""
    ids = [str(d) for d in document_ids]
    need_passport = _batch_stage(batch_id, "extract_batch_meta", lambda: _extract_batch_meta(batch_id, ids))
    header = [analyze_document.si(d) for d in ids]
    if need_passport:
        header.append(extract_passport.si(batch_id, ids))
    if not header:
        return self.replace(finalize_batch.si(batch_id))
    return self.replace(chord(header, finalize_batch.si(batch_id)))


def _extract_batch_meta(batch_id: str, ids: list[str]) -> bool:
    """Предмет закупки для правил: шаблоном по извещению, иначе коротким вызовом LLM.

    Предмет, который уже есть (повторный анализ, правка пользователем), не перезаписывается.
    Возвращает, нужен ли паспорт закупки: его ещё нет, а документы с текстом есть.
    """
    with runtime.session_scope() as s:
        batch = s.get(Batch, pipeline.as_uuid(batch_id))
        subject, has_passport = batch.subject, "passport" in (batch.meta or {})
    documents = [] if subject and has_passport else _ready_documents(ids)
    if not subject and documents:
        subject = ml.extract_batch_meta([(name, blocks) for _, name, blocks in documents], llm=runtime.get_llm())
        if subject:
            with runtime.session_scope() as s:
                meta = pipeline.set_batch_meta(s, batch_id, subject=subject)
            progress.publish_batch_meta(batch_id, meta)
    log.info("extract_batch_meta: пакет %s: предмет закупки: %s", batch_id, subject or "не определён")
    return bool(documents) and not has_passport


@celery_app.task(name="worker.tasks.extract_passport")
def extract_passport(batch_id: str, document_ids: list[str]) -> None:
    """Паспорт закупки (НМЦК, обеспечения, сроки) в `batch.meta`; ошибка LLM оставляет паспорт пустым."""
    _batch_stage(batch_id, "extract_passport", lambda: _extract_passport(batch_id, [str(d) for d in document_ids]))


def _extract_passport(batch_id: str, ids: list[str]) -> None:
    documents = _ready_documents(ids)
    if not documents:
        return
    passport = ml.extract_passport(documents, search=_search([d for d, _, _ in documents]), llm=runtime.get_llm())
    log.info("extract_passport: пакет %s: полей %d", batch_id, len(passport.fields))
    if passport.fields:
        # Пустой паспорт не пишется: повторный анализ попробует ещё раз
        with runtime.session_scope() as s:
            meta = pipeline.set_batch_passport(s, batch_id, passport.model_dump(mode="json"))
        progress.publish_batch_meta(batch_id, meta)


def _group_name(index: int) -> str:
    return f"analysis/group-{index + 1:02d}.json"


@celery_app.task(name="worker.tasks.analyze_document", bind=True)
def analyze_document(self, document_id: str):
    """Правила пакета в версиях со старта → группы по 3–4 → chord групп → `merge_findings`."""
    replacement = _run_stage(self, document_id, "analyze_document", _analyze_document)
    if replacement is not None:
        return self.replace(replacement)
    return None


def _analyze_document(info: DocInfo) -> Signature:
    with runtime.session_scope() as s:
        rules = pipeline.batch_rules(s, info.batch_id)
    plan = [[r.code for r in group] for group in ml.group_rules(rules)]
    _put_json(info, "analysis/plan.json", plan)
    total = sum(len(codes) for codes in plan)
    report_progress(info.id, "analysis", ANALYSIS_START, {"rules_done": 0, "rules_total": total})
    if not plan:
        return merge_findings.si(info.id)  # включённых правил нет: документ без находок ИИ
    log.info("analyze_document: %s: правил %d, групп %d", info.id, total, len(plan))
    return chord([analyze_rule_group.si(info.id, i) for i in range(len(plan))], merge_findings.si(info.id))


@celery_app.task(name="worker.tasks.analyze_rule_group", bind=True)
def analyze_rule_group(self, document_id: str, group: int) -> None:
    """Одна группа правил: вызов LLM, привязка цитат к блокам; кандидаты — в MinIO, БД не пишется."""
    _run_stage(self, document_id, "analyze_rule_group", lambda info: _analyze_rule_group(info, group))


def _analyze_rule_group(info: DocInfo, index: int) -> None:
    plan = _get_json(info, "analysis/plan.json")
    with runtime.session_scope() as s:
        by_code = {r.code: r for r in pipeline.batch_rules(s, info.batch_id)}
        subject = s.get(Batch, pipeline.as_uuid(info.batch_id)).subject
        blocks = pipeline.load_blocks(s, info.id)
    rules = [by_code[code] for code in plan[index] if code in by_code]
    result = ml.analyze_rule_group(blocks, rules, subject=subject, document_id=info.id, search=_search([info.id]),
                                   llm=runtime.get_llm())
    runtime.get_storage().put_bytes(runtime.work_key(info.batch_id, info.id, _group_name(index)),
                                    result.model_dump_json().encode(), content_type="application/json")
    log.info("analyze_rule_group: %s: группа %d (%s): кандидатов %d, отброшено %d, вызовов LLM %d",
             info.id, index + 1, ", ".join(plan[index]), len(result.candidates), len(result.dropped),
             result.usage.calls)
    rules_done(info.id, sum(len(codes) for codes in plan), len(plan[index]), start=ANALYSIS_START,
               span=ANALYSIS_SPAN)


@celery_app.task(name="worker.tasks.merge_findings", bind=True)
def merge_findings(self, document_id: str) -> None:
    """Все группы готовы: находки ИИ документа заменяются, ручные остаются; документ `done`, цвет, счётчики."""
    _run_stage(self, document_id, "merge_findings", _merge_findings)


def _merge_findings(info: DocInfo) -> None:
    plan = _get_json(info, "analysis/plan.json")
    storage = runtime.get_storage()
    results = [RuleGroupResult.model_validate_json(storage.get_bytes(
        runtime.work_key(info.batch_id, info.id, _group_name(i)))) for i in range(len(plan))]
    with runtime.session_scope() as s:
        recalc = pipeline.merge_findings(s, info.id, results)
        event = progress.document_done_event(s, info.id, recalc)
    progress.publish_document_done(info.batch_id, event)
    log.info("merge_findings: %s: находок ИИ %d, цвет %s", info.id, sum(len(r.candidates) for r in results),
             (recalc.get("document") or {}).get("worst_level"))


@celery_app.task(name="worker.tasks.rule_test")
def rule_test(payload: dict) -> dict:
    """`POST /api/rules/test` (FR-9.3): правило из формы на тексте или на документе, не дольше 60 с.

    `payload` — `{"rule": {...RuleSpec}, "text": "..."}` или `{"rule": {...}, "document_id": "..."}`.
    Возвращает `{"result": RuleTestResult}` или `{"error": {code, message, retryable}}`: ошибку ML
    API показывает как есть, а исключение через бэкенд результатов потеряло бы свой класс.
    """
    try:
        rule = RuleSpec.model_validate(payload["rule"])
        document_id = payload.get("document_id")
        if document_id:
            document_id = str(document_id)
            with runtime.session_scope() as s:
                doc = s.get(Document, pipeline.as_uuid(document_id))
                subject = s.get(Batch, doc.batch_id).subject if doc is not None else None
                blocks = pipeline.load_blocks(s, document_id) if doc is not None else []
            result = ml.test_rule(rule, blocks=blocks, document_id=document_id, search=_search([document_id]),
                                  subject=subject, llm=runtime.get_llm())
        else:
            result = ml.test_rule(rule, text=payload.get("text") or "", llm=runtime.get_llm())
    except MLError as exc:
        log.info("rule_test: %s: %s", exc.code, exc)
        return {"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}
    return {"result": result.model_dump(mode="json")}
