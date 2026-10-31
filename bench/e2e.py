"""Сквозной прогон пакета через задачи конвейера Celery: файлы → находки в БД → разметка экспорта.

В отличие от `bench.pipeline`, который зовёт функции ML напрямую, здесь работают те же задачи, что
в воркере (`worker.tasks`), с настоящими PostgreSQL, эмбеддингами и LLM. Celery — в режиме eager:
задачи идут по очереди в одном процессе, поэтому время — верхняя оценка, в воркерах документы и
группы правил идут параллельно. Хранилище и события — в памяти процесса вместо MinIO и Redis. Для каждого прогона
создаётся своя база (миграции Alembic), в конце она удаляется.

    TEST_DATABASE_URL=postgresql+psycopg://b2b:b2b@localhost:55432/postgres \\
        python -m bench.e2e .cache/packages/0373400006826000073/*.docx \\
        --expected bench/packages/0373400006826000073/expected.json --export /tmp/out

LLM — из окружения (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`), как у воркера; заглушка
`devtools/llm_stub` подходит для бесплатной проверки связок.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def create_database(admin_url: str) -> str:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"b2b_e2e_{uuid.uuid4().hex[:8]}"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "db" / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


def drop_database(admin_url: str, url: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from db.session import get_engine

    get_engine.cache_clear()
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{make_url(url).database}" WITH (FORCE)'))
    engine.dispose()


def eager_celery():
    """Задачи в процессе: замена задачи на chord в eager ждёт результат, бэкенд — память процесса."""
    import celery.app.task
    from celery.backends.cache import CacheBackend

    from worker.app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    celery.app.task.denied_join_result = nullcontext
    celery_app._local.backend = CacheBackend(app=celery_app, url="memory://")


def start_batch(files: list[Path], name: str) -> str:
    """Черновик с файлами — как его собирает загрузка API, затем старт пакета."""
    from api.config import ALLOWED_EXTENSIONS
    from api.uploads import guess_mime, inspect_file, object_key
    from db import batches, users
    from db.seed import seed
    from worker import runtime

    storage = runtime.get_storage()
    with runtime.session_scope() as s:
        seed(s)
        author = users.create_user(s, login=f"e2e{uuid.uuid4().hex[:6]}", password="e2e-pass-123",
                                   full_name="Сквозной прогон", role="specialist")
        batch_id = batches.create_draft(s, author_id=author.id)["id"]
        rows = []
        for path in files:
            key = object_key(batch_id, path.name)
            storage.put_bytes(key, path.read_bytes())
            rows.append({"filename": path.name, "size_bytes": path.stat().st_size, "mime": guess_mime(path.name, None),
                         "object_key": key, **inspect_file(path, path.name, ALLOWED_EXTENSIONS)})
        batches.add_files(s, batch_id, rows)
        batches.start_batch(s, batch_id, name=name)
        author_id = author.id
    return str(batch_id), author_id


def export_batch(batch_id: str, author_id, out_dir: Path) -> dict:
    """Выгрузка пакета задачей `export_batch`: zip с размеченными документами и `Сводка.xlsx` → `out_dir`."""
    import io
    import zipfile

    from db import exports
    from worker import exports as export_tasks
    from worker import runtime

    with runtime.session_scope() as s:
        job = exports.create_export(s, scope="batch", target_id=batch_id, actor_id=author_id)
    export_tasks.export_batch.apply(args=(job["export_id"],)).get()
    with runtime.session_scope() as s:
        view = exports.export_view(s, job["export_id"])
    if view["status"] != "done":
        return {"status": view["status"], "error": view["error_message"]}
    data = runtime.get_storage().get_bytes(view["object_key"])
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(out_dir)
        names = zf.namelist()
    return {"status": "done", "files": names}


def stage_seconds(batch_id: str) -> dict[str, float]:
    from sqlalchemy import select

    from db.models import TaskRun
    from worker import runtime

    out: dict[str, float] = defaultdict(float)
    with runtime.session_scope() as s:
        for run in s.scalars(select(TaskRun).where(TaskRun.batch_id == uuid.UUID(batch_id))):
            if run.finished_at:
                out[run.stage] += (run.finished_at - run.started_at).total_seconds()
    return dict(out)


def llm_usage(batch_id: str, documents) -> dict:
    """Токены и стоимость анализа — из результатов групп правил в хранилище."""
    from contracts.analysis import LLMUsage, RuleGroupResult
    from worker import runtime

    usage = LLMUsage()
    storage = runtime.get_storage()
    for doc in documents:
        for i in range(32):
            key = runtime.work_key(batch_id, str(doc.id), f"analysis/group-{i + 1:02d}.json")
            try:
                usage.add(RuleGroupResult.model_validate_json(storage.get_bytes(key)).usage)
            except Exception:  # noqa: BLE001 — групп меньше 32: первой отсутствующей заканчиваем
                break
    return usage.model_dump()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--expected", type=Path, help="эталон {rule, doc, quote}; doc — имя файла без расширения")
    ap.add_argument("--export", type=Path, help="каталог: выгрузка пакета задачей export_batch, zip распаковывается сюда")
    ap.add_argument("--json", type=Path, help="сохранить находки и замеры в JSON")
    ap.add_argument("--keep", action="store_true", help="не удалять базу прогона")
    args = ap.parse_args()

    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        raise SystemExit("нужен TEST_DATABASE_URL: PostgreSQL с pgvector, где можно создать базу")
    url = create_database(admin)
    os.environ["DATABASE_URL"] = url
    try:
        run(args, url)
    finally:
        if not args.keep:
            drop_database(admin, url)
        else:
            print(f"база прогона: {url}")


def run(args, url: str) -> None:
    from sqlalchemy import select

    from bench.pipeline import score
    from db import batches, exports
    from db.models import Batch, Document, Finding
    from storage import MemoryStorage
    from worker import runtime, tasks
    from worker.events import MemoryEventBus

    memory = MemoryStorage()
    runtime.get_storage = lambda: memory
    bus = MemoryEventBus()  # события SSE — в памяти процесса, как в тестах конвейера: Redis не нужен
    runtime.get_events = lambda: bus
    eager_celery()

    batch_id, author_id = start_batch(args.files, name="Сквозной прогон")
    started = time.perf_counter()
    tasks.unpack_batch.apply(args=(batch_id,)).get()
    wall = time.perf_counter() - started

    with runtime.session_scope() as s:
        batch = s.get(Batch, uuid.UUID(batch_id))
        documents = list(s.scalars(select(Document).where(Document.batch_id == batch.id).order_by(Document.filename)))
        view = batches.batch_view(s, batch_id)
        report = {"wall_seconds": round(wall, 1), "stages": stage_seconds(batch_id), "subject": batch.subject,
                  "status": batch.status, "passport": exports.passport_fields(batch.meta), "documents": []}
        found: dict[str, list] = defaultdict(list)
        for doc in documents:
            rows = list(s.scalars(select(Finding).where(Finding.document_id == doc.id)))
            payload = exports.export_payload(s, doc.id)
            items = payload["findings"]
            for f in items:
                code = (f["rule"] or {}).get("code")
                found[Path(doc.filename).stem].append(SimpleNamespace(rule_code=code, quote=f["quote"]))
            report["documents"].append({
                "filename": doc.filename, "status": doc.status, "error": doc.error_message, "pages": doc.pages,
                "worst_level": doc.worst_level, "findings": [
                    {k: f[k] for k in ("level", "quote", "section_number", "section_title", "page", "explanation")}
                    | {"rule": (f["rule"] or {}).get("code")} for f in items],
                "without_section": sum(1 for f in items if not f["section_number"] and not f["page"]),
            })
            assert len(rows) == len(items) or any(r.status == "rejected" for r in rows)
        report["usage"] = llm_usage(batch_id, documents)
        report["counters"] = view.get("counters")
    if args.export:
        report["export"] = export_batch(batch_id, author_id, args.export)

    print(f"Пакет: {report['status']}, {report['wall_seconds']} с от старта до finalize_batch (eager, по очереди)")
    print(f"Предмет закупки: {report['subject']}")
    print("Этапы, с (сумма по документам):", ", ".join(f"{k} {v:.1f}" for k, v in sorted(report["stages"].items())))
    u = report["usage"]
    print(f"LLM анализа: вызовов {u['calls']}, вход {u['prompt_tokens']}, выход {u['completion_tokens']}, ${u['cost']:.4f}")
    print(f"Паспорт: {len(report['passport'])} полей — " + "; ".join(f"{p['label']}: {p['value']}" for p in report["passport"]))
    for d in report["documents"]:
        print(f"\n{d['filename']}: {d['status']}, стр. {d['pages']}, цвет {d['worst_level']}, находок {len(d['findings'])}"
              + (f", ошибка: {d['error']}" if d["error"] else ""))
        for f in d["findings"]:
            where = f"п. {f['section_number']}" if f["section_number"] else f"стр. {f['page']}"
            print(f"  [{f['level']}] {f['rule']} {where}: «{f['quote'][:110]}»")
    if report.get("export"):
        print(f"\nВыгрузка пакета: {report['export']}")
    if args.expected:
        expected = json.loads(args.expected.read_text())
        scores = score(found, expected)
        print("\nСверка с эталоном:")
        for code, sc in scores.items():
            print(f"  {code}: эталон {sc['tp_expected']}/{sc['expected']}, находок {sc['found']}, совпало {sc['tp_found']}")
        report["score"] = scores
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
