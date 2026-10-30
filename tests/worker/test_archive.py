"""Безопасная распаковка zip: path traversal, вложенные архивы, лимиты против zip-бомбы, имена cp866."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from worker.archive import Limits, Unpacker


def make_zip(path: Path, entries: dict[str, bytes], *, utf8: bool = True, encoding: str = "cp866") -> Path:
    buf = io.BytesIO()
    placeholders: dict[bytes, bytes] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (name, data) in enumerate(entries.items()):
            if utf8 or name.isascii():
                zf.writestr(name, data)
                continue
            # Имя в `encoding` без флага UTF-8 (cp866 — архивы русской Windows, utf-8 — `zip` на macOS):
            # ASCII-метка той же длины, затем подмена её байтами в заголовках
            encoded = name.encode(encoding)
            marker = f"{i:0{len(encoded)}d}".encode()
            placeholders[marker] = encoded
            zf.writestr(marker.decode(), data)
    raw = buf.getvalue()
    for marker, encoded in placeholders.items():
        raw = raw.replace(marker, encoded)
    path.write_bytes(raw)
    return path


def nested(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def run(tmp_path: Path, archive: Path, limits: Limits | None = None) -> tuple[dict[str, bytes], Unpacker]:
    got: dict[str, bytes] = {}
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    unpacker = Unpacker(work, on_file=lambda path, local, size: got.__setitem__(path, local.read_bytes()),
                        limits=limits or Limits())
    unpacker.unpack(archive, label=archive.name)
    return got, unpacker


def test_traversal_and_service_entries_are_skipped(tmp_path):
    archive = make_zip(tmp_path / "a.zip", {
        "Пакет/Договор.pdf": b"%PDF-1.4 a",
        "Пакет/../../evil.pdf": b"%PDF-1.4 evil",
        "/abs.pdf": b"%PDF-1.4 abs",
        "C:/win.pdf": b"%PDF-1.4 win",
        "__MACOSX/._x.pdf": b"x",
        "Пакет/Смета.xlsx": b"x",
        "Пакет/": b"",
    })
    got, unpacker = run(tmp_path, archive)
    assert got == {"Пакет/Договор.pdf": b"%PDF-1.4 a"}
    assert not unpacker.problems
    # Временные файлы удалены: на диске не остаётся ничего, имена из архива путями не становятся
    assert list((tmp_path / "work").iterdir()) == []
    assert not (tmp_path / "evil.pdf").exists()


def test_nested_archives_keep_folder_structure(tmp_path):
    inner = nested({"Схема.pdf": b"%PDF-1.4 s", "глубже.zip": nested({"Акт.docx": b"PK docx"})})
    archive = make_zip(tmp_path / "a.zip", {"Пакет/Приложения.zip": inner, "Пакет/Договор.docx": b"PK d"})
    got, unpacker = run(tmp_path, archive)
    assert set(got) == {"Пакет/Приложения/Схема.pdf", "Пакет/Приложения/глубже/Акт.docx", "Пакет/Договор.docx"}
    assert not unpacker.problems


def test_nesting_depth_limit(tmp_path):
    deep = nested({"a.zip": nested({"b.zip": nested({"x.pdf": b"%PDF"})})})
    archive = make_zip(tmp_path / "top.zip", {"l1.zip": deep, "ok.pdf": b"%PDF ok"})
    got, unpacker = run(tmp_path, archive, Limits(max_depth=3))
    assert set(got) == {"ok.pdf"}
    assert [(p.path, p.code) for p in unpacker.problems] == [("top.zip/l1.zip/a.zip/b.zip", "archive_depth")]


def test_zip_bomb_stops_on_real_bytes(tmp_path):
    # 50 МБ нулей сжимаются в десятки килобайт: заголовок не важен, считаются распакованные байты
    archive = make_zip(tmp_path / "bomb.zip", {"a.pdf": b"%PDF small", "b.pdf": b"\0" * (50 * 1024 * 1024)})
    got, unpacker = run(tmp_path, archive, Limits(max_bytes=10 * 1024 * 1024))
    assert set(got) == {"a.pdf"}
    assert [p.code for p in unpacker.problems] == ["archive_limit"]
    assert "10 МБ" in unpacker.problems[0].message
    assert list((tmp_path / "work").iterdir()) == []


def test_bomb_inside_nested_archive_stops_whole_unpack(tmp_path):
    inner = nested({"big.pdf": b"\0" * (5 * 1024 * 1024)})
    archive = make_zip(tmp_path / "top.zip", {"in.zip": inner})
    got, unpacker = run(tmp_path, archive, Limits(max_bytes=1024 * 1024))
    assert got == {}
    assert [(p.path, p.code) for p in unpacker.problems] == [("top.zip", "archive_limit")]


def test_file_count_limit(tmp_path):
    archive = make_zip(tmp_path / "many.zip", {f"{i}.pdf": b"%PDF" for i in range(5)})
    got, unpacker = run(tmp_path, archive, Limits(max_files=3))
    assert len(got) == 3
    assert [p.code for p in unpacker.problems] == ["archive_limit"]


def test_cp866_names(tmp_path):
    archive = make_zip(tmp_path / "a.zip", {"Документы/Договор.pdf": b"%PDF"}, utf8=False)
    got, _ = run(tmp_path, archive)
    assert set(got) == {"Документы/Договор.pdf"}


def test_utf8_names_without_flag(tmp_path):
    archive = make_zip(tmp_path / "a.zip", {"Документы/Проект контракта.docx": b"PK"}, utf8=False, encoding="utf-8")
    assert zipfile.ZipFile(archive).infolist()[0].flag_bits & 0x800 == 0
    got, _ = run(tmp_path, archive)
    assert set(got) == {"Документы/Проект контракта.docx"}


def test_corrupted_archive_is_reported(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"PK\x03\x04 broken")
    got, unpacker = run(tmp_path, archive)
    assert got == {}
    assert [p.code for p in unpacker.problems] == ["corrupted"]


def test_callback_errors_are_not_hidden(tmp_path):
    archive = make_zip(tmp_path / "a.zip", {"x.pdf": b"%PDF"})
    unpacker = Unpacker(tmp_path, on_file=lambda *a: (_ for _ in ()).throw(ConnectionError("minio")))
    try:
        unpacker.unpack(archive, label="a.zip")
    except ConnectionError:
        pass
    else:
        raise AssertionError("сбой хранилища должен пробрасываться, а не считаться повреждением архива")
