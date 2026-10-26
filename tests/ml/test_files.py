from __future__ import annotations

import shutil
import zipfile

import pymupdf
import pytest

from bench.docx_xml import lvl, make_docx, numbering, numpr, p
from ml.convert import convert_to_docx, soffice_convert
from ml.extract.docx_reader import extract_docx
from ml.files import check_file

needs_soffice = pytest.mark.skipif(shutil.which("soffice") is None, reason="нет soffice")


def make_pdf(path, pages=2, password=None):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}")
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        doc.save(path)
    return path


def test_pdf_ok_and_pages(tmp_path):
    result = check_file(make_pdf(tmp_path / "a.pdf", pages=3))
    assert (result.check, result.kind, result.pages) == ("ok", "pdf", 3)


def test_pdf_with_password(tmp_path):
    result = check_file(make_pdf(tmp_path / "a.pdf", password="secret"))
    assert (result.check, result.kind) == ("encrypted", "pdf")
    assert "паролем" in result.message


def test_kind_by_signature_not_extension(tmp_path):
    path = make_pdf(tmp_path / "contract.docx")
    assert check_file(path).kind == "pdf"


def test_docx_zip_and_other_office(tmp_path):
    assert check_file(make_docx(tmp_path / "a.docx", p("x"))).kind == "docx"
    with zipfile.ZipFile(tmp_path / "pack.zip", "w") as zf:
        zf.writestr("folder/readme.txt", "x")
    assert check_file(tmp_path / "pack.zip").model_dump(exclude_none=True) == {"check": "ok", "kind": "zip"}
    with zipfile.ZipFile(tmp_path / "book.xlsx", "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert check_file(tmp_path / "book.xlsx").check == "unsupported"


def test_corrupted_and_unsupported(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.7\n garbage")
    assert check_file(tmp_path / "broken.pdf").check == "corrupted"
    with zipfile.ZipFile(tmp_path / "broken.docx", "w") as zf:
        zf.writestr("word/document.xml", "<w:document><unclosed>")
    broken = check_file(tmp_path / "broken.docx")
    assert (broken.check, broken.kind) == ("corrupted", "docx")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    assert check_file(tmp_path / "image.png").check == "unsupported"
    (tmp_path / "empty.pdf").write_bytes(b"")
    assert check_file(tmp_path / "empty.pdf").check == "corrupted"


def test_rtf_signature(tmp_path):
    (tmp_path / "a.rtf").write_bytes(b"{\\rtf1\\ansi Hello}")
    assert check_file(tmp_path / "a.rtf").kind == "rtf"


@needs_soffice
@pytest.mark.soffice
def test_doc_and_rtf_convert_to_docx_with_numbering(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_CACHE_DIR", str(tmp_path / "cache"))
    from ml.config import get_settings

    get_settings.cache_clear()
    xml = numbering({0: lvl(0, "decimal", "%1.") + lvl(1, "decimal", "%1.%2.")}, {1: 0})
    body = p("Предмет контракта", ppr=numpr(1, 0)) + p("Поставка товара", ppr=numpr(1, 1))
    source = make_docx(tmp_path / "src.docx", body, numbering_xml=xml)

    doc_path = soffice_convert(source, "docx", cache_dir=tmp_path / "c1")  # нормализуем через LibreOffice
    rtf_dir = tmp_path / "rtf"
    rtf_dir.mkdir()
    # Получаем настоящий .doc: сохраняем через LibreOffice фильтром MS Word 97
    import subprocess

    subprocess.run(
        ["soffice", f"-env:UserInstallation={(tmp_path / 'prof').as_uri()}", "--headless",
         "--convert-to", "doc:MS Word 97", "--outdir", str(rtf_dir), str(doc_path)],
        check=True, capture_output=True, timeout=120,
    )
    legacy = next(rtf_dir.glob("*.doc"))
    assert check_file(legacy).model_dump(exclude_none=True) == {"check": "ok", "kind": "doc"}

    converted = convert_to_docx(legacy)
    assert converted.exists() and converted.suffix == ".docx"
    assert convert_to_docx(legacy) == converted  # из кеша
    result = extract_docx(converted, kind="doc")
    assert result.kind == "doc"
    assert [(i.number, i.text) for i in result.paragraphs] == [
        ("1.", "Предмет контракта"),
        ("1.1.", "Поставка товара"),
    ]
    get_settings.cache_clear()
