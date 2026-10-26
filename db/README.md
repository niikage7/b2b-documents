# Слой данных

PostgreSQL + pgvector: таблицы из «Требований к бэкенду» (6 октября), миграции Alembic и функции, которые зовут API и задачи Celery. Это зона Андрея по [распределению требований](../docs/tasks.md); с 27 октября её делает Артём.

Функции принимают сессию SQLAlchemy первым аргументом и не делают `commit`: транзакцией управляет вызывающий. Ответы для экранов — словари в формате «Требований к бэкенду», API отдаёт их как есть. Ошибки — классы из [`errors.py`](errors.py) с полями `status`, `code`, `message` (на русском) и `details`; `to_dict()` даёт тело ответа `{"error": {...}}`.

```python
from db.session import session_scope
from db import batches

with session_scope() as s:
    view = batches.batch_view(s, batch_id)
```

## Запуск

```bash
uv pip install --python .venv/bin/python -e ".[db]"
DATABASE_URL=postgresql+psycopg://b2b:b2b@localhost:5432/b2b .venv/bin/alembic -c db/alembic.ini upgrade head
DATABASE_URL=... DEMO_PASSWORD=... .venv/bin/python -m db.seed
```

Сид создаёт профиль «44-ФЗ, поставка товаров» с правилами R-01…R-08 и трёх демо-пользователей (`specialist`, `lawyer`, `methodologist`) с паролем из `DEMO_PASSWORD`. Повтор ничего не дублирует. Образ БД — `pgvector/pgvector:pg17`: миграция сама создаёт расширения `vector` и `pg_trgm`.

Тесты слоя данных нужен PostgreSQL с pgvector; без `TEST_DATABASE_URL` они пропускаются:

```bash
docker run -d --name b2b-test-pg -e POSTGRES_USER=b2b -e POSTGRES_PASSWORD=b2b -p 55432:5432 pgvector/pgvector:pg17
TEST_DATABASE_URL=postgresql+psycopg://b2b:b2b@localhost:55432/postgres .venv/bin/python -m pytest tests/db
```

Каждый прогон создаёт свою базу и накатывает на неё миграции — так проверяется и сама миграция.

## Конвейер: функции для задач Celery

| Задача | Функция | Что делает |
| --- | --- | --- |
| `unpack_batch` | `batches.checkable_files(s, batch_id)`, `pipeline.create_document(s, ...)`, `batches.mark_processing(s, batch_id)` | Файлы черновика → строки `document` |
| Любой этап | `pipeline.start_task_run`, `pipeline.finish_task_run`, `pipeline.set_stage(s, doc_id, stage, progress=, stage_detail=)` | Журнал этапов и прогресс; `set_stage` возвращает данные события `document.progress` со `stage_label` |
| `extract_text` | `pipeline.save_extraction_info(s, doc_id, result)` | Страницы, `kind = scan`, `ocr_confidence`, `low_quality_pages` |
| `build_structure` | `ml.build_structure(result, document_id=...)` → `pipeline.save_blocks(s, doc_id, blocks)` | Блоки с разделами. id блоков детерминированы: повтор не трогает ручные пометки |
| `index_chunks` | `pipeline.load_blocks` → `ml.index_chunks(blocks, document_id=...)` → `pipeline.save_chunks` | Фрагменты с эмбеддингами |
| `extract_batch_meta` | `pipeline.set_batch_meta(s, batch_id, subject=, meta=)` | Предмет закупки; возвращает данные события `batch.meta` |
| `analyze_document` | `pipeline.batch_rules(s, batch_id)` → `ml.group_rules` | Правила в версиях, зафиксированных на старте пакета |
| `analyze_rule_group` | `ml.analyze_rule_group(pipeline.load_blocks(...), group, search=PgVectorSearch(conn), ...)` | Кандидаты в находки, БД не пишет |
| `merge_findings` | `pipeline.merge_findings(s, doc_id, results)` | Находки ИИ документа заменяются, ручные остаются; документ `done`; возвращает `recalc` — данные события `document.done` |
| Документ упал | `pipeline.fail_document(s, doc_id, code, message)` | Статус `error`, причина на русском |
| `finalize_batch` | `pipeline.finish_batch(s, batch_id)` | Статус пакета и счётчики; данные события `batch.done` |
| Повтор документа | `documents.retry_document(s, doc_id)` | Этап, с которого перезапускать (по `task_run`) |

Сырые абзацы (`ExtractionResult`) и слова OCR между задачами в БД не хранятся: их удобнее класть в MinIO JSON-файлом (`model_dump_json()`) рядом с исходником.

## API: функции для эндпоинтов

| Эндпоинт | Функция |
| --- | --- |
| `POST /api/auth/login` | `users.authenticate(s, login, password)` → `users.me(user)`; 401, 403, 429 — исключения с `details.retry_after_seconds` |
| `GET /api/me` | `users.me(users.get_user(s, user_id))` |
| `GET /api/batches` | `batches.list_batches(s, user_id=, search=, status=, level=, period=, owner=, page=, page_size=)` |
| `POST /api/batches` | `batches.create_draft(s, author_id=)` |
| `POST /api/batches/{id}/files` | Файл в MinIO, `ml.check_file`, затем `batches.add_files(s, batch_id, [...])`; 413 — `BatchTooLarge` |
| `DELETE /api/batches/{id}/files/{file_id}` | `batches.remove_file` |
| `POST /api/batches/{id}/start` | `batches.start_batch(s, batch_id, name=, profile_id=)` — фиксирует версии правил |
| `GET /api/batches/{id}`, `batch.snapshot` | `batches.batch_view(s, batch_id)` |
| `PATCH /api/batches/{id}` | `batches.rename_batch(s, batch_id, name=, subject=)` |
| `DELETE /api/batches/{id}` | `batches.delete_batch` — возвращает ключи MinIO черновика для удаления |
| `POST /api/batches/{id}/rerun` | `batches.rerun_batch` — id документов для повторного анализа |
| `GET /api/documents/{id}` | `documents.document_view(s, doc_id, viewer_id=)` |
| `GET /api/documents/{id}/blocks` | `documents.document_blocks(s, doc_id, from_page=, to_page=)` |
| `GET /api/documents/{id}/search` | `documents.search_document(s, doc_id, q)` |
| `GET /api/documents?status=done&limit=20` | `documents.recent_documents(s, limit=)` |
| `PATCH /api/findings/{id}` | `findings.update_finding(s, id, actor_id=, version=, status=, level=, explanation=, comment=)` |
| `POST /api/documents/{id}/findings` | `findings.create_user_finding(s, doc_id, actor_id=, block_id=, start=, end=, level=, comment=)` |
| `DELETE /api/findings/{id}` | `findings.delete_user_finding(s, id, actor_id=)` |
| `GET /api/findings/{id}/history` | `findings.finding_history(s, id)` |
| `GET /api/batches/{id}/findings` | `documents.batch_findings(s, batch_id, level=, rule_id=, status=, document_id=, page=, page_size=)` |
| Экспорт | `exports.create_export`, `exports.finish_export`, `exports.export_view`; данные — `exports.export_payload(s, doc_id)` и `exports.batch_summary(s, batch_id)` |
| Профили | `rules.list_profiles`, `create_profile`, `update_profile`, `delete_profile` |
| Правила | `rules.list_rules`, `get_rule`, `create_rule`, `update_rule(s, id, data, version=)`, `delete_rule`, `system_variables` |
| Очистка черновиков | `batches.expired_drafts(s)` — черновики старше 24 часов и ключи их файлов |

`create_rule` и `update_rule` проверяют правило так же, как `ml.validate_rule`, плюс уникальность кода в профиле и лимит в 5 правил «Проверка ИИ» (`too_many_llm_rules`). `query_embedding` можно передать (`ml.embed_rule_query`) или не передавать: движок посчитает эмбеддинг строки поиска сам.

## Что отличается от «Требований к бэкенду»

| Что | Почему |
| --- | --- |
| Таблица `batch_rule` | Версии правил, которыми проверяется пакет, фиксируются на старте: правка правила не меняет запущенные пакеты |
| `batch.meta` | Паспорт закупки из извещения для шапки пакета |
| `finding.recommendation` | Что поставщику сделать с риском |
| `block.stream`, `block.page_end` | Сноски не образуют разделов; абзац может перейти на следующую страницу |
| У `chunk` нет индекса HNSW | Поиск идёт внутри одного документа: фрагменты читаются целиком и ранжируются гибридом векторов и BM25 в `ml.analysis.search` |
| `finding_event.actor_id` может быть пустым | События, которые записала система (находки ИИ); в истории показывается «ИИ» |
