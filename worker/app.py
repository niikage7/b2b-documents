"""Приложение Celery: брокер и бэкенд результатов — Redis, четыре очереди (FR-2.1).

Запуск воркеров (пул только prefork: PyMuPDF не работает в нескольких потоках):

    celery -A worker.app worker -Q ingest,analysis,export --pool prefork
    celery -A worker.app worker -Q ocr --pool prefork
"""

from __future__ import annotations

import os

from celery import Celery
from kombu import Queue

QUEUES = ("ingest", "ocr", "analysis", "export")

# Задача → очередь. Всё, что не указано, идёт в ingest
ROUTES = {
    "worker.tasks.ocr_pages": "ocr",
    "worker.tasks.index_chunks": "analysis",
    "worker.tasks.analyze_document": "analysis",
}


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or "redis://localhost:6379/0"


celery_app = Celery("b2b", include=["worker.tasks"])
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
    # Redis вернёт неподтверждённую задачу в очередь через час: дольше не работает ни одна задача
    broker_transport_options={"visibility_timeout": 3600},
    result_backend_transport_options={"visibility_timeout": 3600},
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    # Модель эмбеддингов и кеш LibreOffice держат память: процесс пула перезапускается время от времени
    worker_max_tasks_per_child=int(os.environ.get("WORKER_MAX_TASKS_PER_CHILD") or 200),
    worker_hijack_root_logger=False,
    timezone="UTC",
)
