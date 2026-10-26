# Базовый образ воркеров ingest, ocr и analysis: Tesseract (Debian trixie), LibreOffice,
# шрифты и веса модели эмбеддингов. Итоговые замеры OCR делаются внутри этого образа:
# версия Tesseract меняет результат при тех же моделях.
#
# Сборка из корня репозитория:
#   docker build -f ml/Dockerfile -t b2b-ml .
#   docker build -f ml/Dockerfile --build-arg EMBEDDING_MODEL=deepvk/USER-bge-m3 -t b2b-ml .
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_THREAD_LIMIT=1 \
    HF_HOME=/opt/hf \
    ML_CACHE_DIR=/var/cache/b2b-ml

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tesseract-ocr-osd \
        libreoffice-writer-nogui \
        fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch без CUDA ставится отдельно, иначе pip подтянет сборку с CUDA на несколько гигабайт
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml ./
COPY contracts ./contracts
COPY ml ./ml
RUN pip install ".[ml,embeddings,pg]"

ARG EMBEDDING_MODEL=sergeyzh/BERTA
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL'])"

RUN mkdir -p "$ML_CACHE_DIR" && tesseract --version && tesseract --list-langs
