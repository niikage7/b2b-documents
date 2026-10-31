"""Конвейер от файлов пакета до блоков: задачи Celery в режиме eager на настоящем PostgreSQL.

Файлы — синтетические из `bench.samples` и `bench.scangen`; OCR и LibreOffice подменяются
(в тестовом окружении их может не быть), LLM не вызывается.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import func, select

import ml
from bench import samples
from contracts.extraction import BBox, OcrPage, OcrWord
from db.models import Batch, BlockRow, ChunkRow, Document, TaskRun
from tests.worker.conftest import make_batch, sample_bytes, zip_bytes
from worker import tasks


def docs_of(s, batch_id) -> dict[str, Document]:
    s.expire_all()
    return {d.original_path: d for d in s.scalars(select(Document).where(Document.batch_id == batch_id))}


def count(s, model, document_id) -> int:
    return s.scalar(select(func.count()).select_from(model).where(model.document_id == document_id))


def run_batch(batch_id: str) -> None:
    tasks.unpack_batch.apply(args=(batch_id,)).get()


def pdf_with_password() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Secret")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="pw", user_pw="pw")


@pytest.fixture
def docx_bytes():
    return sample_bytes(samples.make_contract_docx, ".docx")


@pytest.fixture
def notice_pdf(fonts):
    return sample_bytes(samples.make_notice_pdf, ".pdf")


def test_docx_and_pdf_reach_done(s, env, author, docx_bytes, notice_pdf):
    batch_id = make_batch(s, env, author, [("Договор.docx", docx_bytes), ("Извещение.pdf", notice_pdf),
                                            ("Смета.xlsx", b"junk")])
    run_batch(batch_id)

    docs = docs_of(s, batch_id)
    assert set(docs) == {"Договор.docx", "Извещение.pdf"}  # неподдерживаемый файл документом не становится
    for doc in docs.values():
        assert (doc.status, doc.stage, doc.progress) == ("done", "done", 100), doc.error_message
        assert doc.pages and doc.pages >= 1
        assert count(s, BlockRow, doc.id) > 5
        assert count(s, ChunkRow, doc.id) > 0
    assert docs["Договор.docx"].kind == "docx" and docs["Извещение.pdf"].kind == "pdf"
    assert s.get(Batch, docs["Договор.docx"].batch_id).status == "done"

    # Текст документа виден так же, как его отдаёт API редактору
    from db import documents

    blocks = documents.document_blocks(s, docs["Договор.docx"].id, from_page=1, to_page=20)
    text = " ".join(b["text"] for b in blocks["blocks"])
    assert "Контракт" in text or "контракт" in text

    stages = set(s.scalars(select(TaskRun.stage).where(TaskRun.batch_id == docs["Договор.docx"].batch_id)))
    assert {"unpack_batch", "extract_text", "build_structure", "index_chunks", "analyze_document",
            "finalize_batch"} <= stages
    assert not s.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.status != "done"))
    # Промежуточный результат извлечения лежит в хранилище рядом с исходниками пакета
    doc = docs["Договор.docx"]
    assert f"batches/{batch_id}/work/{doc.id}/extraction.json" in env.objects


def test_rerun_of_pipeline_does_not_duplicate(s, env, author, docx_bytes):
    batch_id = make_batch(s, env, author, [("Договор.docx", docx_bytes)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Договор.docx"]
    blocks, chunks = count(s, BlockRow, doc.id), count(s, ChunkRow, doc.id)
    block_ids = set(s.scalars(select(BlockRow.id).where(BlockRow.document_id == doc.id)))

    run_batch(batch_id)  # повтор всего конвейера: те же документы и блоки, без дублей
    docs = docs_of(s, batch_id)
    assert list(docs) == ["Договор.docx"] and docs["Договор.docx"].id == doc.id
    assert count(s, BlockRow, doc.id) == blocks and count(s, ChunkRow, doc.id) == chunks
    assert set(s.scalars(select(BlockRow.id).where(BlockRow.document_id == doc.id))) == block_ids
    assert docs["Договор.docx"].status == "done"


def test_zip_with_nested_archive_and_traversal(s, env, author, docx_bytes, notice_pdf):
    inner = zip_bytes({"Схема.pdf": notice_pdf})
    archive = zip_bytes({
        "Закупка/Битый.pdf": b"not a pdf",
        "Закупка/Договор.docx": docx_bytes,
        "Закупка/Извещение.pdf": notice_pdf,
        "Закупка/Приложения.zip": inner,
        "Закупка/../evil.pdf": notice_pdf,
        "Закупка/readme.txt": b"x",
    })
    batch_id = make_batch(s, env, author, [("Закупка.zip", archive)])
    run_batch(batch_id)

    docs = docs_of(s, batch_id)
    broken = docs.pop("Закупка/Битый.pdf")
    assert (broken.status, broken.error_code) == ("error", "corrupted")
    assert set(docs) == {"Закупка/Договор.docx", "Закупка/Извещение.pdf", "Закупка/Приложения/Схема.pdf"}
    assert {d.status for d in docs.values()} == {"done"}
    assert docs["Закупка/Приложения/Схема.pdf"].filename == "Схема.pdf"
    # Распакованные файлы — в MinIO под ключом из id документа
    for doc in docs.values():
        assert doc.object_key.startswith(f"batches/{batch_id}/unpacked/{doc.id}.")
        assert doc.object_key in env.objects

    run_batch(batch_id)  # повтор распаковки находит те же документы
    assert set(docs_of(s, batch_id)) == set(docs) | {"Закупка/Битый.pdf"}


def test_zip_bomb_becomes_error_document(s, env, author, docx_bytes, monkeypatch):
    monkeypatch.setenv("UNPACK_MAX_MB", "1")
    archive = zip_bytes({"a/Договор.docx": docx_bytes, "a/Огромный.pdf": b"%PDF-1.4" + b"\0" * (3 * 1024 * 1024)})
    batch_id = make_batch(s, env, author, [("Бомба.zip", archive)])
    run_batch(batch_id)

    docs = docs_of(s, batch_id)
    assert docs["a/Договор.docx"].status == "done"
    bomb = docs["Бомба.zip"]
    assert (bomb.status, bomb.kind, bomb.error_code) == ("error", "zip", "archive_limit")
    assert "1 МБ" in bomb.error_message
    assert s.get(Batch, bomb.batch_id).status == "partial_error"


def test_failed_document_does_not_stop_batch(s, env, author, docx_bytes, notice_pdf):
    batch_id = make_batch(s, env, author, [("Договор.docx", docx_bytes), ("Битый.pdf", notice_pdf),
                                            ("Пароль.pdf", pdf_with_password())])
    # Файл прошёл проверку при загрузке, а в хранилище оказался повреждён
    broken = next(k for k, v in env.objects.items() if v == notice_pdf)
    env.objects[broken] = b"%PDF-1.4 broken"
    run_batch(batch_id)

    docs = docs_of(s, batch_id)
    assert docs["Договор.docx"].status == "done"
    assert (docs["Битый.pdf"].status, docs["Битый.pdf"].error_code) == ("error", "corrupted")
    assert docs["Битый.pdf"].error_message  # причина на русском — из ml.errors
    assert (docs["Пароль.pdf"].status, docs["Пароль.pdf"].error_code) == ("error", "encrypted")
    assert s.get(Batch, docs["Договор.docx"].batch_id).status == "partial_error"
    # Упавший этап записан в журнал: с него документ перезапустит этап 6
    run = s.scalars(select(TaskRun).where(TaskRun.document_id == docs["Битый.pdf"].id)).one()
    assert (run.stage, run.status) == ("extract_text", "error")


def test_internal_error_is_document_error(s, env, author, docx_bytes, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("неожиданный сбой")

    monkeypatch.setattr(ml, "build_structure", boom)
    batch_id = make_batch(s, env, author, [("Договор.docx", docx_bytes)])
    run_batch(batch_id)
    doc = docs_of(s, batch_id)["Договор.docx"]
    assert (doc.status, doc.error_code) == ("error", "internal_error")
    assert "внутренней ошибки" in doc.error_message
    assert count(s, ChunkRow, doc.id) == 0  # следующие этапы упавшего документа пропущены


def test_rtf_goes_through_conversion(s, env, author, docx_bytes, tmp_path, monkeypatch):
    converted = tmp_path / "converted.docx"
    converted.write_bytes(docx_bytes)
    calls = []

    def fake_convert(path, **kwargs):
        calls.append(Path(path).suffix)
        return converted

    monkeypatch.setattr(ml, "convert_to_docx", fake_convert)
    rtf = rb"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times;}} \f0 Contract \par }"
    batch_id = make_batch(s, env, author, [("Договор.rtf", rtf)])
    run_batch(batch_id)

    doc = docs_of(s, batch_id)["Договор.rtf"]
    assert doc.status == "done", doc.error_message
    assert calls == [".rtf"] and doc.kind == "rtf"
    assert doc.normalized_key == f"batches/{batch_id}/work/{doc.id}/converted.docx"
    assert env.objects[doc.normalized_key] == docx_bytes
    assert count(s, BlockRow, doc.id) > 5


def fake_ocr_from(text_pdf: bytes, calls: list[list[int]]):
    """OCR без Tesseract: слова страницы берутся из текстового слоя исходного PDF, уверенность 0,97."""

    def ocr(path, pages, *, progress=None, **kwargs):
        calls.append(list(pages))
        out = []
        with pymupdf.open(stream=io.BytesIO(text_pdf)) as doc:
            for i, no in enumerate(pages, start=1):
                page = doc[no - 1]
                words = [OcrWord(text=w[4], bbox=BBox(x0=w[0], y0=w[1], x1=w[2], y1=w[3]),
                                 layout=BBox(x0=w[0], y0=w[1], x1=w[2], y1=w[3]), confidence=0.97,
                                 block=w[5], par=0, line=w[6])
                         for w in page.get_text("words")]
                out.append(OcrPage(page=no, width=page.rect.width, height=page.rect.height, words=words,
                                   confidence=0.97))
                if progress:
                    progress(i, len(pages))
        return out

    return ocr


def test_scan_pages_go_to_ocr_in_groups_of_five(s, env, author, fonts, tmp_path, monkeypatch):
    from bench.scangen import ScanStyle, make_scan

    src = tmp_path / "contract.pdf"
    samples.make_contract_pdf(src, items=samples.long_contract(7))
    text_pdf = src.read_bytes()
    pages = pymupdf.open(src).page_count
    assert pages >= 6
    scan = make_scan(src, tmp_path / "scan.pdf", style=ScanStyle(dpi=100))
    calls: list[list[int]] = []
    monkeypatch.setattr(ml, "ocr_pages", fake_ocr_from(text_pdf, calls))

    batch_id = make_batch(s, env, author, [("Скан.pdf", scan.read_bytes())])
    run_batch(batch_id)

    doc = docs_of(s, batch_id)["Скан.pdf"]
    assert doc.status == "done", doc.error_message
    assert calls == [list(range(i, min(i + 5, pages + 1))) for i in range(1, pages + 1, 5)]
    assert (doc.kind, doc.pages) == ("scan", pages)
    assert doc.ocr_confidence == pytest.approx(0.97)
    assert count(s, BlockRow, doc.id) > 20 and count(s, ChunkRow, doc.id) > 0
    stages = [r for r in s.scalars(select(TaskRun.stage).where(TaskRun.document_id == doc.id))]
    assert stages.count("ocr_pages") == len(calls) and "build_pdf_result" in stages
    for group in calls:
        assert f"batches/{batch_id}/work/{doc.id}/ocr/{group[0]:04d}-{group[-1]:04d}.json" in env.objects


def test_ocr_progress_counts_pages_across_groups(s, env, author, fonts, tmp_path, monkeypatch):
    from worker.progress import ocr_page_done

    batch_id = make_batch(s, env, author, [("Извещение.pdf", sample_bytes(samples.make_notice_pdf, ".pdf"))])
    tasks.unpack(batch_id)
    doc = next(iter(docs_of(s, batch_id).values()))
    events = [ocr_page_done(str(doc.id), 7, 1, start=10, span=50) for _ in range(3)]
    events.append(ocr_page_done(str(doc.id), 7, 9, start=10, span=50))  # повтор группы не уводит за 7
    assert [e["stage_detail"]["page"] for e in events] == [1, 2, 3, 7]
    assert events[-1]["stage_label"] == "Распознавание текста, стр. 7 из 7" and events[-1]["progress"] == 60


def test_rerun_batch_reanalyses_documents(s, env, author, docx_bytes):
    from db import batches

    batch_id = make_batch(s, env, author, [("Договор.docx", docx_bytes)])
    run_batch(batch_id)
    ids = batches.rerun_batch(s, batch_id)
    assert s.get(Batch, docs_of(s, batch_id)["Договор.docx"].batch_id).status == "queued"
    tasks.rerun_batch(batch_id, ids)
    doc = docs_of(s, batch_id)["Договор.docx"]
    assert doc.status == "done" and s.get(Batch, doc.batch_id).status == "done"


def test_ocr_groups():
    assert tasks.ocr_groups([1, 2, 3, 4, 5, 6, 7], 5) == [[1, 2, 3, 4, 5], [6, 7]]
    assert tasks.ocr_groups([], 5) == []
