# Образ API: лёгкий, без torch, OpenCV и Tesseract. Проверка файла при загрузке (ml.check_file) и
# проверка правила (ml.validate_rule) работают на PyMuPDF и lxml; тяжёлая работа уходит в очереди Celery.
#
# Сборка из корня репозитория:
#   docker build -f api/Dockerfile -t b2b-api .
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY contracts ./contracts
COPY ml ./ml
COPY db ./db
COPY storage ./storage
COPY worker ./worker
COPY api ./api
# pymupdf 2.x тянет onnxruntime, networkx и pymupdf-layout для разметки страниц; check_file ими не пользуется
RUN pip install ".[api,db]" \
    && pip uninstall -y onnxruntime pymupdf-layout pymupdf4llm networkx \
    && python -c "import sys; from ml import check_file, validate_rule; import api.app, db.seed, worker.tasks; \
assert 'torch' not in sys.modules and 'cv2' not in sys.modules"

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=4).status == 200 else 1)"
CMD ["sh", "-c", "python -m api.bootstrap && exec uvicorn --factory api.app:create_app --host 0.0.0.0 --port 8000"]
