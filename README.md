# Светофор рисков

Умный помощник для тендерных специалистов: проверяет закупочную документацию по настраиваемому чек-листу и показывает риски в формате светофора — с цитатой, разделом документа и объяснением.

Решение для кейс-чемпионата B2B-РТС, Задача 1 «Светофор рисков документов».

## Запуск

Нужен только Docker с Compose. Первая сборка занимает несколько минут: образ воркеров содержит LibreOffice, Tesseract и модель эмбеддингов.

1. Настройки. Без файла `.env` всё работает на значениях для разработки и на заглушке LLM: бесплатно, без ключа, но находки не настоящие. Свои пароли, секрет сессии и порты задаются в `.env` по шаблону:

   ```bash
   cp .env.example .env
   ```

2. Чтобы проверять документы настоящей моделью, задайте в `.env` значения `LLM_BASE_URL`, `LLM_MODEL` и `LLM_API_KEY`. В шаблоне уже указаны OpenRouter и модель; остаётся ключ. Если `.env` скопирован из шаблона, а ключа нет, удалите из `.env` строки `LLM_BASE_URL` и `LLM_MODEL`: иначе воркеры пойдут в OpenRouter без ключа и документы получат ошибку «LLM недоступна».

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
| http://localhost:5555 | Flower: очереди и задачи Celery, воркеры, повторы (порт — `FLOWER_PORT`) |
| http://localhost:9001 | Консоль MinIO (порт — `MINIO_CONSOLE_PORT`) |

Все адреса открыты только с этой машины (127.0.0.1); интерфейс можно открыть в локальную сеть через `FRONTEND_BIND=0.0.0.0`.

Интерфейс можно запустить и без бэкенда, на встроенном моке API, — см. [frontend/README.md](frontend/README.md).

### Из чего состоит окружение

| Сервис | Что делает |
| --- | --- |
| `db` | PostgreSQL 17 + pgvector: пакеты, документы, блоки текста, фрагменты с эмбеддингами, находки, правила |
| `redis` | Брокер Celery, бэкенд результатов и поток событий пакета для SSE |
| `minio` | Файлы: исходники, промежуточные результаты обработки, готовые выгрузки |
| `api` | FastAPI; при старте накатывает миграции и создаёт демо-пользователей и чек-лист по умолчанию |
| `worker` | Celery, очереди `ingest`, `analysis`, `export`: распаковка, извлечение текста, фрагменты, анализ по правилам, выгрузка; планировщик очистки (черновики старше суток, просроченные выгрузки) |
| `worker-ocr` | Celery, очередь `ocr`: сканы распознаются группами по 5 страниц параллельно |
| `flower` | Мониторинг очередей Celery |
| `frontend` | Интерфейс за nginx; `/api` проксируется на `api`, поток событий — без буферизации |
| `llm-stub` | Заглушка LLM, совместимая с OpenAI API: к ней ходят воркеры, пока в `.env` не задан настоящий провайдер |

Тяжёлая работа идёт только в очередях: загрузка и запуск отвечают сразу (за доли секунды), прогресс приходит по SSE. Ошибка одного документа не останавливает пакет: временные сбои (связь, LLM) повторяются с паузой до трёх раз, упавший документ можно перезапустить кнопкой «Повторить».

Журнал API и воркеров — JSON, одна строка на запись, с `batch_id`, `document_id` и `export_id` (`LOG_FORMAT=text` — обычный текст):

```bash
docker-compose logs -f worker worker-ocr
```

### Скорость

Замер на синтетических документах (`bench.samples`, `bench.scangen`), с заглушкой LLM, на ноутбуке под Docker Desktop (WSL 2), запросы — через nginx интерфейса. Время ответа настоящей модели сюда не входит: с ней анализ добавляет несколько секунд на группу правил.

| Пакет | Обработка |
| --- | --- |
| PDF с текстом, 20 страниц | 16 с (первый пакет после запуска — 32 с: загрузка модели эмбеддингов) |
| Скан, 20 страниц | 38 с, из них распознавание — 23 с |
| PDF с текстом, 79 страниц + скан, 60 страниц (82 МБ) | 150 с; загрузка файлов — 0,9 с, ни один ответ API не дольше 0,05 с |

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

Один размеченный документ: DOCX с заливкой и комментариями Word (DOC и RTF — тоже в DOCX) или PDF с выделениями и заметками. Статус и скачивание — те же `GET /api/exports/{id}` и `/download`; файл хранится сутки:

```bash
curl -b cookies.txt -X POST -H 'Content-Type: application/json' -d '{}' http://localhost:8000/api/documents/$DOCUMENT_ID/exports
```

Перезапустить документ со статусом «Ошибка» с этапа, на котором он упал, и проверить готовый пакет заново по актуальным правилам (ручные пометки сохраняются):

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/documents/$DOCUMENT_ID/retry
```

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/batches/$BATCH_ID/rerun
```

Новое правило без изменения кода (роль `methodologist`): проверить черновик на своём тексте или на документе (`"document_id"` вместо `"text"`), затем сохранить. Правило действует на пакеты, запущенные после сохранения:

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"login": "methodologist", "password": "demo"}' http://localhost:8000/api/auth/login
```

```bash
curl -b cookies.txt -H 'Content-Type: application/json' -d '{
  "rule": {"code": "R-90", "title": "Короткий срок поставки", "description": "Срок поставки меньше 10 дней",
           "level": "warning", "kind": "semantic_llm",
           "prompt_template": "Фрагмент договора: {фрагмент}. Риск, если срок поставки меньше 10 календарных дней."},
  "text": "Поставка товара осуществляется в течение 3 (трёх) календарных дней с даты заключения Контракта."
}' http://localhost:8000/api/rules/test
```

Сохранить то же правило в профиль по умолчанию (`profile_id` — в другой профиль) и посмотреть список:

```bash
curl -b cookies.txt -H 'Content-Type: application/json' -d '{
  "code": "R-90", "title": "Короткий срок поставки", "description": "Срок поставки меньше 10 дней",
  "level": "warning", "kind": "semantic_llm",
  "prompt_template": "Фрагмент договора: {фрагмент}. Риск, если срок поставки меньше 10 календарных дней."
}' http://localhost:8000/api/rules
```

```bash
curl -b cookies.txt http://localhost:8000/api/rules
```

Полный список эндпоинтов и схемы ответов — http://localhost:8000/api/docs.

## Документация

| Документ | Что внутри |
| --- | --- |
| [docs/hackathon.md](docs/hackathon.md) | Задание, формат, критерии приёмки, требования к стеку и развёртыванию |
| [docs/tasks.md](docs/tasks.md) | Требования, распределение по команде, общие контракты |
| [docs/interface.md](docs/interface.md) | Экраны, компоненты и состояния интерфейса |
| [docs/glossary.md](docs/glossary.md) | Термины закупок и продукта |
| [CLAUDE.md](CLAUDE.md) | Контекст и правила для работы с ИИ-ассистентами |
