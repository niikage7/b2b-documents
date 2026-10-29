# Образ воркеров Celery: поверх образа ML (ml/Dockerfile — Tesseract, LibreOffice, torch CPU, веса
# эмбеддингов) добавляются слой данных, клиент MinIO и задачи конвейера. Один образ на сервисы
# worker (очереди ingest, analysis, export) и worker-ocr (очередь ocr).
#
# В docker-compose базовый образ собирается сервисом ml-base и передаётся сюда контекстом «ml».
# Отдельная сборка из корня репозитория:
#   docker build -f ml/Dockerfile -t b2b-ml .
#   docker build -f worker/Dockerfile --build-context ml=docker-image://b2b-ml -t b2b-worker .
FROM ml

COPY pyproject.toml ./
COPY contracts ./contracts
COPY ml ./ml
COPY db ./db
COPY storage ./storage
COPY worker ./worker
# torch и модель уже в базовом образе: здесь доставляются только SQLAlchemy, Celery и клиент MinIO
RUN pip install ".[ml,embeddings,pg,db,worker]" \
    && python -c "import worker.tasks, ml.ocr.pages, ml.structure"

RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p "$ML_CACHE_DIR" && chown -R app "$ML_CACHE_DIR"
USER app

ENV WORKER_QUEUES=ingest,analysis,export \
    WORKER_CONCURRENCY=2
# Пул только prefork: PyMuPDF не работает в нескольких потоках одного процесса
HEALTHCHECK --interval=30s --timeout=20s --start-period=60s --retries=5 \
    CMD celery -A worker.app inspect ping -d "celery@$HOSTNAME" --timeout 10 > /dev/null || exit 1
CMD ["sh", "-c", "exec celery -A worker.app worker -Q \"$WORKER_QUEUES\" --pool prefork --concurrency \"$WORKER_CONCURRENCY\" -n \"celery@$HOSTNAME\" --loglevel INFO"]
