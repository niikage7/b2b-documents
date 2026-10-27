# Открытые пакеты 44-ФЗ: разметка для стендов

Три пакета документации электронных аукционов на поставку товаров с zakupki.gov.ru (приём заявок на 27.10.2026). Файлы в репозиторий не входят: они лежат в `.cache/packages/<номер>/` (каталог в `.gitignore`). Результаты прогонов — в [docs/ml-plan.md](../../docs/ml-plan.md#проверка-на-открытых-пакетах-2710).

| Закупка | Предмет | Заказчик |
| --- | --- | --- |
| [0135100003926000021](https://zakupki.gov.ru/epz/order/notice/ea20/view/documents.html?regNumber=0135100003926000021) | Поставка мебели | Прокуратура Калининградской области |
| [0338200009926000090](https://zakupki.gov.ru/epz/order/notice/ea20/view/documents.html?regNumber=0338200009926000090) | Поставка моющих средств | КГКУ «ЦОД», Камчатский край |
| [0373400006826000073](https://zakupki.gov.ru/epz/order/notice/ea20/view/documents.html?regNumber=0373400006826000073) | Поставка персональных компьютеров и оргтехники | ФГБУ «ЦНИГРИ» |

## Что в каталоге пакета

| Файл | Что внутри |
| --- | --- |
| `expected.json` | Ожидаемые находки `{rule, doc, quote}` для `bench.pipeline --expected`: условия, которые нарушают пороги чек-листа `r44_default.json` |
| `disputed.json` | Спорные условия: находку можно засчитать, но и пропуск не ошибка. Стенд их не читает, они нужны для ручного разбора точности |
| `search.json` | Пункты, которые правило `semantic_llm` должно прочитать, чтобы решить, есть ли риск (размер обеспечения, пени, сроки, гарантия), — для `bench.embeddings --set package` |

`doc` — имя файла без расширения: `contract` — проект контракта, `notice` — извещение, `spec` — описание объекта закупки (ТЗ), `requirements` — требования к содержанию заявки. Обоснование НМЦК (XLS, XLSX) не берётся.

## Файлы

Скачиваются со страницы «Документы» закупки: `https://zakupki.gov.ru/44fz/filestore/public/1.0/download/priz/file.html?uid=<uid>`. Сайт работает на сертификате Russian Trusted Root CA (Минцифры), которого нет в хранилищах macOS и Linux. Корневой сертификат — `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`, SHA-256 `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`; его передают только этим запросам: `curl --cacert russian_trusted_root_ca_pem.crt …`.

| Пакет | Файл | uid | SHA-256 |
| --- | --- | --- | --- |
| 0135100003926000021 | `notice.docx` — «Извещение №08- ЭА26 - мебель.docx» | `01A1681215CE705286FA37919D2F0326` | `c7fdfdc6123b1795a7bfb008c25071d7314613cf957420fc74f1551027b37769` |
| | `spec.docx` — «Приложение №1 Описание объекта закупки.docx» | `01A16811A2197C9D8D793C28B79D3FDC` | `2fedf6b790da2eba3b83cda9737f8febd9474e60aa366f4662c5c8da7f230c8b` |
| | `contract.doc` — «Приложение №4. Проект государственного контракта.doc» | `01A168116DE37985BA656AA6689DEF47` | `288325d57c6eef3e6346d8baff1cd594c93eb949fb86b5c22a20115309f5bd13` |
| | `requirements.docx` — «Приложение №3. Требования к содержанию, составу заявки…» | `01A16811CDCF78CF9429ACA6FD409CEF` | `2814d7a47cf33e8ded3a9d55b467e653b78a64bd02864eec522faea3cd441146` |
| 0338200009926000090 | `spec.docx` — «Приложение к извещению 1____ТЗ.docx» | `01A175831DB67C5994F77A65FC87AAE4` | `198d2fcffebce4a7fdbef846424818ea81eabe2aa69ad66d1a874fec9d8558f6` |
| | `contract.docx` — «Приложение к извещению 3____Проект контракта.docx» | `01A175830B767DB18BFAAE062BD024B8` | `b511917dd61692af42063260940e81e760d356d1a66cd3a1beb90b49ab7e7ba7` |
| | `requirements.docx` — «Приложение к извещению 4____Требования к заявке.docx» | `01A1758337647E689D18AA8A42CE0838` | `127423aec9f37356cd1d89b30a70cf4e7248c9d661957a2a8ec209b122771e98` |
| 0373400006826000073 | `spec.docx` — «II. Описание объекта закупки2.docx» (последняя редакция) | `01A168CE45837F3C92EF61CDFC09EB6D` | `a66cbcaf3642f1ed6ede6371cfe318638e9911d98c9a6fd4cf3bf319b98471ad` |
| | `contract.docx` — «IV. ПРОЕКТ КОНТРАКТА.docx» | `01A15DBE4DD477388529AA9FE1D123A4` | `0e1e0753e3d1f194cd0e4aa522f6b4a530a01106619970da1fbc66b5d1e0f327` |
| | `requirements.docx` — «I. Требование к содержанию заявки.docx» | `01A15DBEABEE7F1F8756C85EA39DC12E` | `89b104fe4a895feb498a9556e21546569789f935d963ac47cc9d587d5440fe74` |

**Извещение для 0338… и 0373….** У этих заказчиков извещение — только структурированная форма ЕИС. Печатная форма `https://zakupki.gov.ru/epz/order/notice/printForm/view.html?regNumber=<номер>` сохранена в DOCX: из HTML убраны `<script>`, `<style>`, ссылки и картинки, затем

```bash
soffice --headless --infilter="HTML (StarWriter)" --convert-to "docx:MS Word 2007 XML" notice.html
```

В PDF печатную форму не конвертировать: LibreOffice выводит правую колонку таблиц за край листа, и значения полей обрезаются.

## Прогон

```bash
P=.cache/packages/0338200009926000090
python -m bench.pipeline $P/contract.docx $P/notice.docx $P/spec.docx $P/requirements.docx \
    --expected bench/packages/0338200009926000090/expected.json
python -m bench.embeddings sergeyzh/BERTA intfloat/multilingual-e5-base --set package \
    --expected bench/packages/0338200009926000090/search.json \
    --doc contract=$P/contract.docx --doc notice=$P/notice.docx --doc spec=$P/spec.docx --doc requirements=$P/requirements.docx
```

Анализ одного пакета — около 105 тыс. токенов входа и $0,017–0,021 на `deepseek-v4.1-flash-1017`. Для пакетов с пустым `expected.json` стенд таблицу полноты не печатает: все находки разбираются вручную по `disputed.json`.
