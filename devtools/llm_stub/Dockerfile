# Заглушка LLM для разработки: только стандартная библиотека Python, без зависимостей.
# Сборка: docker build -t b2b-llm-stub devtools/llm_stub
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLM_STUB_PORT=8080

WORKDIR /app
COPY server.py ./server.py

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8080
CMD ["python", "/app/server.py"]
