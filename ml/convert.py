"""Конвертация через LibreOffice headless: DOC и RTF → DOCX, DOCX → PDF для стенда нумерации.

Каждый вызов получает свой профиль LibreOffice: на общем профиле параллельные
конвертации блокируют друг друга. Процесс запускается в своей группе, по таймауту
завершается вместе с дочерними. Результат кешируется по хешу файла.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from ml.config import get_settings
from ml.errors import ConversionError

log = logging.getLogger(__name__)

_FILTERS = {
    "docx": "docx:MS Word 2007 XML",
    "pdf": "pdf:writer_pdf_Export",
}


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def soffice_convert(
    path: str | Path,
    target: str,
    *,
    cache_dir: Path | None = None,
    timeout: float | None = None,
) -> Path:
    """Конвертирует файл в формат `target` («docx» или «pdf») и возвращает путь в кеше."""
    settings = get_settings()
    src = Path(path)
    cache_dir = (cache_dir or settings.cache_dir) / "convert"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(src)
    out = cache_dir / f"{digest}.{target}"
    if out.exists():
        return out

    soffice = shutil.which(settings.soffice_bin) or settings.soffice_bin
    timeout = timeout or settings.convert_timeout
    with tempfile.TemporaryDirectory(prefix="lo_") as tmp:
        tmp_path = Path(tmp)
        # Своё имя входного файла: LibreOffice плохо переносит пробелы и кириллицу в путях на части систем
        src_copy = tmp_path / f"input{src.suffix.lower() or '.bin'}"
        shutil.copyfile(src, src_copy)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        cmd = [
            soffice,
            f"-env:UserInstallation={(tmp_path / 'profile').as_uri()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            _FILTERS[target],
            "--outdir",
            str(out_dir),
            str(src_copy),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ConversionError(detail=f"нет {soffice}") from exc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            raise ConversionError(detail=f"таймаут {timeout:.0f} с") from exc
        result = out_dir / f"input.{target}"
        if proc.returncode != 0 or not result.exists():
            raise ConversionError(
                detail=f"код {proc.returncode}: {stderr.decode(errors='replace')[-500:]}"
            )
        tmp_out = out.with_suffix(f".{os.getpid()}.tmp")
        shutil.move(str(result), tmp_out)
        os.replace(tmp_out, out)
    log.info("soffice: %s → %s", src.name, out.name)
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def convert_to_docx(path: str | Path, **kwargs) -> Path:
    """DOC или RTF → DOCX. Экспорт для таких документов открывает этот же DOCX."""
    return soffice_convert(path, "docx", **kwargs)
