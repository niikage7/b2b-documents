"""Тесты задач конвейера: настоящий PostgreSQL (`TEST_DATABASE_URL`), Celery в режиме eager, без сети и LLM.

Задачи открывают сессии через `worker.runtime.session_scope`: здесь это точка сохранения внутри
транзакции теста, поэтому всё откатывается в конце. Хранилище — `MemoryStorage`, эмбеддинги и LLM —
подделки (`fake_llm` отвечает по плану теста, без сети).
"""

from __future__ import annotations

import io
import tempfile
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from tests.db.conftest import pg_url, s  # noqa: F401 — фикстуры БД используются и здесь
from tests.fakes import FakeEmbedder, FakeLLM

WINDOWS_FONTS = {"BENCH_FONT_SERIF": "C:/Windows/Fonts/times.ttf", "BENCH_FONT_SERIF_BOLD": "C:/Windows/Fonts/timesbd.ttf"}


class Embedder768(FakeEmbedder):
    dim = 768


@pytest.fixture
def storage():
    from storage import MemoryStorage

    return MemoryStorage()


@pytest.fixture
def fake_llm():
    """LLM задач конвейера; тест задаёт план находок: `fake_llm.plan = {"R-02": [(цитата, пояснение)]}`."""
    return FakeLLM()


@pytest.fixture
def env(s, storage, fake_llm, monkeypatch):  # noqa: F811
    import celery.app.task
    from celery.backends.cache import CacheBackend

    from ml.analysis.embeddings import set_default_embedder
    from worker import runtime
    from worker.app import celery_app

    @contextmanager
    def scope():
        savepoint = s.begin_nested()
        try:
            yield s
        except BaseException:
            savepoint.rollback()
            raise
        savepoint.commit()

    monkeypatch.setattr(runtime, "session_scope", scope)
    monkeypatch.setattr(runtime, "get_storage", lambda: storage)
    monkeypatch.setattr(runtime, "get_llm", lambda: fake_llm.client())
    conf = celery_app.conf
    saved = conf.task_always_eager, conf.task_eager_propagates
    conf.task_always_eager, conf.task_eager_propagates = True, True
    # apply_async в eager запрещает ждать результат внутри задачи, а замена задачи на chord в eager
    # как раз ждёт его (`sig.apply().get()`); на Redis замены идут без ожидания, запрет там не нужен
    monkeypatch.setattr(celery.app.task, "denied_join_result", nullcontext)
    # chord в режиме eager всё равно обращается к бэкенду результатов: вместо Redis — память процесса
    saved_backend = getattr(celery_app._local, "backend", None)
    celery_app._local.backend = CacheBackend(app=celery_app, url="memory://")
    set_default_embedder(Embedder768())
    yield storage
    set_default_embedder(None)
    conf.task_always_eager, conf.task_eager_propagates = saved
    if saved_backend is None:
        del celery_app._local.backend
    else:
        celery_app._local.backend = saved_backend


@pytest.fixture
def fonts(monkeypatch):
    """Шрифт с кириллицей для синтетических PDF из `bench.samples`; на Windows — Times New Roman."""
    from bench.fonts import font_path

    if font_path("serif") is None:
        if not Path(WINDOWS_FONTS["BENCH_FONT_SERIF"]).exists():
            pytest.skip("нет шрифта с кириллицей для синтетического PDF")
        for name, value in WINDOWS_FONTS.items():
            monkeypatch.setenv(name, value)


@pytest.fixture
def author(s):  # noqa: F811
    from db import users
    from db.seed import seed

    seed(s)
    return users.create_user(s, login=f"w{uuid.uuid4().hex[:6]}", password="worker-pass-1", full_name="Тест Конвейер",
                             role="specialist")


def make_batch(s, storage, author, files: list[tuple[str, bytes]], *, name: str = "Тест") -> str:  # noqa: F811
    """Черновик с файлами так же, как его собирает загрузка API, затем запуск."""
    from api.config import ALLOWED_EXTENSIONS
    from api.uploads import guess_mime, inspect_file, object_key
    from db import batches

    batch_id = batches.create_draft(s, author_id=author.id)["id"]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for filename, data in files:
            path = Path(tmp) / f"{uuid.uuid4().hex}{Path(filename).suffix}"
            path.write_bytes(data)
            checked = inspect_file(path, filename, ALLOWED_EXTENSIONS)
            key = object_key(batch_id, filename)
            storage.put_bytes(key, data)
            rows.append({"filename": filename, "size_bytes": len(data), "mime": guess_mime(filename, None),
                         "object_key": key, **checked})
    batches.add_files(s, batch_id, rows)
    batches.start_batch(s, batch_id, name=name)
    return batch_id


def zip_bytes(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def sample_bytes(maker, suffix: str, **kwargs) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"sample{suffix}"
        maker(path, **kwargs)
        return path.read_bytes()

