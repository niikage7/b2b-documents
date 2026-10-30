"""Приложение Celery: брокер и бэкенд результатов — Redis, четыре очереди (FR-2.1).

Запуск воркеров (пул только prefork: PyMuPDF не работает в нескольких потоках):

    celery -A worker.app worker -Q ingest,analysis,export --pool prefork
    celery -A worker.app worker -Q ocr --pool prefork

Периодическая очистка (`worker.maintenance`) ставится Celery beat: в docker-compose он работает внутри
сервиса `worker` (`--beat`, один экземпляр). Журнал — `worker.jsonlog` (`LOG_FORMAT=json` в compose).
"""

from __future__ import annotations

import inspect
import os

from celery import Celery, signals
from kombu import Queue

from worker import jsonlog

QUEUES = ("ingest", "ocr", "analysis", "export")

# Задача → очередь. Всё, что не указано, идёт в ingest
ROUTES = {
    "worker.tasks.ocr_pages": "ocr",
    "worker.tasks.index_chunks": "analysis",
    "worker.tasks.extract_batch_meta": "analysis",
    "worker.tasks.analyze_document": "analysis",
    "worker.tasks.analyze_rule_group": "analysis",
    "worker.tasks.merge_findings": "analysis",
    "worker.tasks.extract_passport": "analysis",
    "worker.tasks.check_package": "analysis",
    "worker.tasks.rule_test": "analysis",
    "worker.exports.export_document": "export",
    "worker.exports.export_batch": "export",
    "worker.exports.export_batch_document": "export",
    "worker.exports.build_batch_zip": "export",
    "worker.maintenance.cleanup_expired": "ingest",
}


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or "redis://localhost:6379/0"


VISIBILITY_TIMEOUT = int(os.environ.get("CELERY_VISIBILITY_TIMEOUT") or 3600)
CLEANUP_INTERVAL_MINUTES = float(os.environ.get("CLEANUP_INTERVAL_MINUTES") or 60)

celery_app = Celery("b2b", include=["worker.tasks", "worker.exports", "worker.maintenance"])
celery_app.conf.update(
    broker_url=os.environ.get("CELERY_BROKER_URL") or _redis_url(),
    # Бэкенд результатов нужен chord: счётчик завершённых задач группы хранится в Redis
    result_backend=os.environ.get("CELERY_RESULT_BACKEND") or _redis_url(),
    result_expires=24 * 3600,
    task_queues=[Queue(name) for name in QUEUES],
    task_default_queue="ingest",
    task_routes={name: {"queue": queue} for name, queue in ROUTES.items()},
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Задача подтверждается после выполнения: если воркер упал посреди работы, её получит другой
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Длинная задача (OCR группы страниц) не держит в запасе чужие задачи
    worker_prefetch_multiplier=1,
    # Воркер убит без остановки: Redis вернёт неподтверждённую задачу в очередь через час — дольше
    # не работает ни одна задача, иначе её выполнили бы дважды
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT},
    result_backend_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT},
    # Остановка воркера (FR-2.3): по SIGQUIT (в compose — stop_signal) задачам даётся столько секунд,
    # затем незаконченные отменяются и возвращаются в очередь — их доделает этот же воркер после запуска
    # или другой воркер очереди; без этого `docker compose stop` ждал бы долгую задачу или убивал её.
    # REMAP_SIGTERM=SIGQUIT не использовать: процессы пула тогда игнорируют сигнал отмены задачи
    worker_soft_shutdown_timeout=float(os.environ.get("WORKER_SOFT_SHUTDOWN_SECONDS") or 20),
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    # События задач для Flower: без них он видит воркеры, но не задачи
    worker_send_task_events=True,
    task_send_sent_event=True,
    # Модель эмбеддингов и кеш LibreOffice держат память: процесс пула перезапускается время от времени
    worker_max_tasks_per_child=int(os.environ.get("WORKER_MAX_TASKS_PER_CHILD") or 200),
    worker_hijack_root_logger=False,
    timezone="UTC",
    # Очистка черновиков старше суток и просроченных выгрузок; задача короткая и идемпотентная
    beat_schedule={
        "cleanup-expired": {
            "task": "worker.maintenance.cleanup_expired",
            "schedule": CLEANUP_INTERVAL_MINUTES * 60,
            "options": {"expires": CLEANUP_INTERVAL_MINUTES * 60},
        },
    },
)

# --- Журнал: JSON с идентификаторами пакета, документа и выгрузки ------------------------------

IDS = ("batch_id", "document_id", "export_id")


@signals.setup_logging.connect
def _setup_logging(loglevel=None, **_):
    """Celery не настраивает журнал сам: корневой логгер пишет в stdout в формате `LOG_FORMAT`."""
    jsonlog.configure("worker", level=loglevel or None)


def task_ids(task, args, kwargs) -> dict:
    """Идентификаторы из аргументов задачи по именам параметров: `document_id`, `batch_id`, `export_id`."""
    try:
        bound = inspect.signature(task.run).bind_partial(*(args or ()), **(kwargs or {}))
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in bound.arguments.items() if k in IDS and isinstance(v, str)}


@signals.task_prerun.connect
def _bind_task_context(sender=None, task_id=None, args=None, kwargs=None, **_):
    jsonlog.clear()
    jsonlog.bind(task=getattr(sender, "name", None), task_id=task_id, **task_ids(sender, args, kwargs))


@signals.task_postrun.connect
def _clear_task_context(**_):
    jsonlog.clear()
