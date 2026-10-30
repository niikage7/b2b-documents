# Светофор рисков

Умный помощник для тендерных специалистов: проверяет закупочную документацию по настраиваемому чек-листу и показывает риски в формате светофора — с цитатой, разделом документа и объяснением.

Решение для кейс-чемпионата B2B-РТС, Задача 1 «Светофор рисков документов».

## Запуск

Нужен только Docker с Compose. Первая сборка занимает несколько минут: образ воркеров содержит LibreOffice, Tesseract и модель эмбеддингов.

1. Скопируйте настройки. Без правок всё работает на заглушке LLM: бесплатно, без ключа, но находки не настоящие.

   ```bash
   cp .env.example .env
   ```

2. Чтобы проверять документы настоящей моделью, задайте в `.env` значения `LLM_BASE_URL`, `LLM_MODEL` и `LLM_API_KEY`. В шаблоне уже указаны OpenRouter и модель; остаётся ключ. Чтобы остаться на заглушке, удалите из `.env` строки `LLM_BASE_URL` и `LLM_MODEL`.

3. Запустите окружение:

   ```bash
   docker-compose up --build
   ```

4. Откройте интерфейс: http://localhost:8080. Вход — `specialist`, `lawyer` или `methodologist`, пароль `demo` (меняется в `DEMO_PASSWORD`). Правила редактирует только `methodologist`.

5. На экране «Загрузить пакет» выберите файлы или zip-архив (pdf, docx, doc, rtf, до 100 МБ) и нажмите «Начать проверку». Откройте готовый документ, чтобы увидеть находки в тексте, принять или отклонить их и скачать разметку.

| Адрес | Что там |
| --- | --- |
| http://localhost:8080 | Интерфейс (порт — `FRONTEND_PORT`) |
| http://localhost:8000/api/docs | Описание API, Swagger (порт — `API_PORT`) |
| http://localhost:9001 | Консоль MinIO (порт — `MINIO_CONSOLE_PORT`) |

Интерфейс можно запустить и без бэкенда, на встроенном моке API, — см. [frontend/README.md](frontend/README.md).

## Примеры запросов к API

Вход сохраняет сессию в cookie, остальные запросы идут с ней. Ошибки приходят в формате `{"error": {"code", "message", "details"}}`, текст — по-русски.

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"login": "specialist", "password": "demo"}' http://localhost:8000/api/auth/login
```

Создать черновик пакета, загрузить в него файлы и запустить проверку; в ответе на первый запрос — `id` пакета:

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/batches
```

```bash
curl -b cookies.txt -F files=@contract.docx -F files=@notice.pdf http://localhost:8000/api/batches/$BATCH_ID/files
```

```bash
curl -b cookies.txt -H 'Content-Type: application/json' -d '{"name": "Закупка 0001"}' http://localhost:8000/api/batches/$BATCH_ID/start
```

Следить за обработкой (SSE) и получить результат с документами и счётчиками:

```bash
curl -N -b cookies.txt http://localhost:8000/api/batches/$BATCH_ID/events
```

```bash
curl -b cookies.txt http://localhost:8000/api/batches/$BATCH_ID
```

Текст документа с находками и решение по находке; `version` берётся из самой находки:

```bash
curl -b cookies.txt http://localhost:8000/api/documents/$DOCUMENT_ID
```

```bash
curl -b cookies.txt -X PATCH -H 'Content-Type: application/json' \
  -d '{"version": 1, "status": "accepted"}' http://localhost:8000/api/findings/$FINDING_ID
```

Выгрузка пакета: zip с размеченными документами и `Сводка.xlsx`. Запрос ставит задачу в очередь, статус содержит `download_url`:

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/batches/$BATCH_ID/exports
```

```bash
curl -b cookies.txt http://localhost:8000/api/exports/$EXPORT_ID
```

```bash
curl -b cookies.txt -OJ http://localhost:8000/api/exports/$EXPORT_ID/download
```

## Документация

| Документ | Что внутри |
| --- | --- |
| [docs/hackathon.md](docs/hackathon.md) | Задание, формат, критерии приёмки, требования к стеку и развёртыванию |
| [docs/tasks.md](docs/tasks.md) | Требования, распределение по команде, общие контракты |
| [docs/interface.md](docs/interface.md) | Экраны, компоненты и состояния интерфейса |
| [docs/glossary.md](docs/glossary.md) | Термины закупок и продукта |
| [CLAUDE.md](CLAUDE.md) | Контекст и правила для работы с ИИ-ассистентами |
