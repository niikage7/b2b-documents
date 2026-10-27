# ML-часть

Извлечение текста, OCR, эмбеддинги, поиск, LLM и движок правил — зона Артёма по [плану](../docs/ml-plan.md). Код — обычные функции Python без зависимости от Celery: вход — путь к файлу или данные, выход — данные по контрактам из [`contracts/`](../contracts). Задачи Celery оборачивают их, тесты и стенды зовут напрямую.

Уровни находок (`critical`, `warning`) и типы правил (`semantic_llm`, `llm`) взяты из документа «Требования к бэкенду» от 6 октября: он заменяет часть решений из [распределения требований](../docs/tasks.md).

## Функции для задач Celery

| Задача | Очередь | Функция | Что возвращает |
| --- | --- | --- | --- |
| Загрузка файла в черновик | API | `ml.check_file(path)` | `FileCheck`: `check` (`ok`, `unsupported`, `encrypted`, `corrupted`), `kind`, `message` на русском |
| `extract_text`, DOCX | ingest | `ml.extract_docx(path)` | `ExtractionResult` с сырыми абзацами и картой runs |
| `extract_text`, DOC и RTF | ingest | `ml.convert_to_docx(path)`, затем `ml.extract_docx(docx, kind="doc", source_pages=ml.doc_page_count(path))` | То же; экспорт открывает этот же сконвертированный DOCX. `source_pages` — число страниц из свойств DOC: без него страницы оцениваются по объёму текста |
| `extract_text`, PDF | ingest | `ml.plan_pdf(path)` | Классы страниц (`PageInfo`) и `needs_ocr` у страниц для OCR |
| `ocr_pages` | ocr | `ml.ocr_pages(path, pages, progress=..., images_dir=...)` | `OcrPage` на страницу: только слова с рамками и уверенностью |
| После всех групп OCR | ingest | `ml.build_pdf_result(path, plan, ocr)` | `ExtractionResult`: абзацы собираются один раз, иначе абзац на границе групп разорвётся |
| `build_structure` | ingest | `ml.build_structure(result, document_id=...)` | Блоки (`contracts.document.Block`) с `section_number`, `section_title`, `section_block_id`; `id` детерминированы — повтор этапа даёт те же блоки |
| `index_chunks` | analysis | `ml.index_chunks(blocks, document_id=...)` | Строки таблицы `chunk` (`ChunkRecord`) с эмбеддингами и `page_from`, `page_to`; у окна длинного блока — `char_start`, `char_end` |
| Сохранение правила | API | `ml.validate_rule(rule)`, `ml.embed_rule_query(rule)` | Ошибки для 422 и `rule_version.query_embedding` |
| `extract_batch_meta` | analysis | `ml.extract_batch_meta([(filename, blocks), ...])` | Предмет закупки или `None` |
| `extract_passport` | analysis, параллельно с анализом | `ml.extract_passport([(document_id, filename, blocks), ...], search=PgVectorSearch(conn))` | `ProcurementPassport`: НМЦК, обеспечения, сроки поставки, приёмки и оплаты, гарантия, место поставки, дата окончания подачи заявок — у каждого значения цитата и место в документе. Пишется `db.pipeline.set_batch_passport` в `batch.meta` |
| Старт анализа | analysis | `ml.group_rules(rules)` | Группы по 3–4 правила для chord |
| `analyze_rule_group` | analysis | `ml.analyze_rule_group(blocks, rules, subject=..., document_id=..., search=PgVectorSearch(conn))` | `RuleGroupResult`: кандидаты с `block_id`, `start`, `end`, отброшенные ответы, учёт токенов. Поиск читает все фрагменты документа и сам ранжирует их гибридом векторов и BM25 |
| Запрос о даче разъяснений | export | `ml.draft_clarification(findings, subject=, customer=, procurement_number=, law=)`, затем `draft.as_text()` или `ml.render_clarification_docx(draft, path, sender=)` | Письмо заказчику по находкам пакета: все вопросы в одном запросе, цитата и пункт — из находки, формулировку вопроса пишет LLM. Вход — `db.exports.clarification_input(s, batch_id, finding_ids=)` |
| `POST /api/rules/test` | API | `ml.test_rule(rule, text=...)` или `ml.test_rule(rule, blocks=..., document_id=..., search=...)` | `RuleTestResult`, не дольше 60 с |
| Сид БД | — | `ml.load_default_checklist()` | Профиль «44-ФЗ, поставка товаров» и правила R-01…R-08 |

Ошибки — классы из [`ml/errors.py`](errors.py) с полями `code` и `message` (текст на русском, его показывают как есть) и признаком `retryable`. PyMuPDF не работает в нескольких потоках: всё, что открывает PDF, должно идти в воркерах с пулом процессов. Прогресс OCR отдаётся колбэком `progress(done, total)` — его удобно связать с `report_progress`.

`analyze_rule_group` принимает любые блоки с полями `id`, `order`, `text`, `number`, `page`, `section_number` — например, Pydantic-модели блока. Раздел находки `merge_findings` берёт из блока `block_id`, а не из ответа LLM.

## Что внутри

| Каталог | Что делает |
| --- | --- |
| `files.py`, `convert.py` | Тип файла по сигнатуре, пароль и повреждение; LibreOffice со своим профилем на вызов, таймаутом и кешем |
| `extract/docx_*.py`, `extract/numbering.py` | Обход XML DOCX: правки, поля, `w:sdt`, надписи, формулы, сноски; автонумерация по правилам Word; таблицы с объединёнными ячейками и признаком раскладки; страницы по меткам Word или оценкой |
| `extract/pdf_*.py`, `extract/layout.py` | Строки по геометрии, колонки XY-сечением, колонтитулы, знаки сносок, битый текстовый слой, номер пункта по шаблону, склейка абзаца через страницу |
| `extract/page_classes.py` | Классы страниц `text`, `scan`, `mixed`, `curves`, `empty` (FR-3.4) |
| `ocr/` | Края, ориентация, наклон, линии таблиц; Tesseract `--psm 3`; перечитывание оторванных номеров пунктов; чистка; уверенность страницы (FR-3.5); кеш с версией конвейера |
| `structure.py` | Блоки с разделами из сырых абзацев (FR-3.1, FR-4.2) и фрагменты для поиска |
| `analysis/` | Шаблоны и переменные правил, эмбеддинги, гибридный поиск (векторы и BM25), адаптер LLM, промпты, привязка цитат, движок, `test_rule`, предмет закупки, паспорт закупки |
| `checklist/` | Стартовый чек-лист R-01…R-08 |

Любая правка подготовки или чистки OCR увеличивает `PIPELINE_VERSION` в [`config.py`](config.py), иначе кеш отдаст старое распознавание.

## Запуск тестов и стендов

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[ml,embeddings,pg,dev]"
.venv/bin/python -m pytest
```

Тесты OCR нужны Tesseract с языками `rus` и `eng`; без них они пропускаются. Локально модель `rus` можно положить в `.cache/tessdata/` вместе с `eng` и `osd` — тесты подхватят её сами. Тест конвертации пропускается без `soffice`.

| Стенд | Команда | Что показывает |
| --- | --- | --- |
| Точность OCR | `python -m bench.ocr_cer [pdf ...]` | 1 − CER на «качественных сканах», сгенерированных из PDF с текстовым слоем; правила нормализации — в начале файла |
| Генератор сканов | `python -m bench.scangen src.pdf scan.pdf` | Скан 300 dpi: серый фон, наклон до 0,6°, шум, JPEG |
| Нумерация DOCX | `python -m bench.numbering file.docx` | Расхождения наших номеров с рендером LibreOffice |
| Выбор bi-encoder и поиска | `python -m bench.embeddings model ... [--chunk-tokens 60,100,400]` | Доля пунктов с риском в фрагментах правила при бюджете токенов: векторный поиск, BM25, гибрид; скорость на CPU |
| Сквозной прогон | `python -m bench.pipeline --pages 20 --scan` | Время этапов, находки, полнота и точность по правилам, токены и стоимость |
| Рассуждения модели | `python -m bench.pipeline --pages 20 --reasoning default,low,none --repeat 3` | Время анализа, токены рассуждений, стоимость, полнота и точность в каждом режиме `LLM_REASONING`; без кеша LLM, режимы чередуются |
| Модели решений и LLM | `python -m bench.decisions [--context 4] [--variant prompt,desc]` | Jev и Clef против LLM на размеченных пунктах `bench/clauses.py`: AUC, полнота и точность, доля пар для LLM без потерь, задержка, стоимость |
| Снимки парсера | `UPDATE_SNAPSHOTS=1 pytest tests/ml/test_snapshots.py` | Обновить эталонные снимки после ожидаемого изменения разбора |

Без своих файлов стенды берут синтетический пакет из [`bench/samples.py`](../bench/samples.py): проект контракта и извещение с заложенными рисками R-01…R-08. Это не реальная закупка: цифры на нём проверяют конвейер, решения о моделях и порогах принимаются на открытых пакетах с zakupki.gov.ru.

Образ воркеров — [`ml/Dockerfile`](Dockerfile), базовый образ — Debian trixie.

## Ограничения MVP

- Таблицы на сканах не восстанавливаются: ячейки идут обычными абзацами, одиночные цифры в узких ячейках могут теряться.
- Смешанная страница распознаётся целиком; слова OCR поверх слов текстового слоя отбрасываются.
- Страницы DOCX без меток Word оцениваются по объёму текста (`page_estimated`). Число страниц подгоняется под `docProps/app.xml` (у DOC — под свойства исходного файла), как и страницы по меткам Word, если меток заметно меньше или больше, чем страниц.
- Колонтитулы DOCX не читаются; сноски идут отдельными абзацами в конце.

## Что согласовать

| С кем | Вопрос |
| --- | --- |
| Влад | Контракт 2 ([`contracts/extraction.py`](../contracts/extraction.py)) — черновик: поля `RawParagraph`, карта runs и рамки слов |
| Влад | `ocr_pages` возвращает только слова, абзацы собирает `build_pdf_result` после всех групп — это меняет контракт 3 |
| Влад | Рамки в системе координат `page.get_text`: подсветка `add_highlight_annot` на странице с `/Rotate 90` проверена тестом |
