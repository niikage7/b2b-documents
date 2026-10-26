"""Синтетический пакет закупки: проект контракта и извещение с заложенными рисками.

Тексты написаны для тестов и стендов, это не реальная закупка. Риски заложены под
стартовый чек-лист R-01…R-08; `EXPECTED` — ожидаемые находки для метрик и сквозных
тестов. Документы собираются в DOCX (с автонумерацией) и в PDF (своей вёрсткой).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from bench.docx_xml import lvl, make_docx, numbering, numpr, p, r
from bench.fonts import font_path


@dataclass(frozen=True)
class Item:
    level: int  # 0 — раздел, 1 — пункт, 2 — подпункт, -1 — абзац без номера
    text: str
    bold: bool = False


CONTRACT_TITLE = "КОНТРАКТ № 0373100000126000045 на поставку серверного оборудования"

CONTRACT: list[Item] = [
    Item(-1, "г. Москва", False),
    Item(-1, "Федеральное казённое учреждение «Центр обработки данных», именуемое в дальнейшем "
             "«Заказчик», в лице директора Иванова Петра Сергеевича, действующего на основании Устава, "
             "с одной стороны, и общество с ограниченной ответственностью «Северный терминал», именуемое "
             "в дальнейшем «Поставщик», в лице генерального директора Смирнова Олега Викторовича, "
             "действующего на основании Устава, с другой стороны, заключили настоящий Контракт о нижеследующем."),
    Item(0, "Предмет контракта", True),
    Item(1, "Поставщик обязуется поставить серверное оборудование (далее — Товар) в количестве и по "
            "характеристикам, указанным в Спецификации (Приложение № 1), а Заказчик обязуется принять и "
            "оплатить Товар в порядке и на условиях Контракта."),
    Item(1, "Поставляемый Товар: сервер Lenovo ThinkSystem SR650 V3 в количестве 4 (четырёх) штук, "
            "модуль памяти Kingston KSM48R40BD4TMM-64HMR в количестве 32 (тридцати двух) штук."),
    Item(1, "Товар должен быть новым, не бывшим в эксплуатации, не восстановленным, свободным от прав "
            "третьих лиц."),
    Item(0, "Цена контракта и порядок расчётов", True),
    Item(1, "Цена Контракта составляет 4 850 000 (четыре миллиона восемьсот пятьдесят тысяч) рублей "
            "00 копеек, в том числе НДС 20 %."),
    Item(1, "Оплата поставленного Товара производится в течение 30 (тридцати) рабочих дней с даты "
            "подписания Заказчиком документа о приёмке."),
    Item(1, "Аванс по Контракту не предусмотрен."),
    Item(0, "Сроки и порядок поставки", True),
    Item(1, "Поставка Товара осуществляется в течение 3 (трёх) календарных дней с даты заключения "
            "Контракта. Досрочная поставка допускается."),
    Item(1, "Место поставки Товара: г. Москва, ул. Строителей, д. 12, склад № 2. Площадь склада "
            "составляет 120 м²."),
    Item(1, "Поставщик за свой счёт осуществляет доставку, погрузочно-разгрузочные работы и подъём "
            "Товара на этаж."),
    Item(0, "Порядок приёмки", True),
    Item(1, "Приёмка Товара осуществляется в течение 25 (двадцати пяти) рабочих дней с даты "
            "поставки. Для проверки соответствия Товара Заказчик проводит экспертизу; расходы на "
            "проведение экспертизы несёт Поставщик."),
    Item(1, "При обнаружении недостатков Поставщик обязан устранить их в течение 1 (одного) рабочего "
            "дня с даты получения требования Заказчика."),
    Item(0, "Обеспечение исполнения контракта", True),
    Item(1, "Размер обеспечения исполнения Контракта составляет 30 (тридцать) процентов от начальной "
            "(максимальной) цены Контракта."),
    Item(1, "Обеспечение исполнения Контракта предоставляется в виде независимой гарантии или "
            "внесением денежных средств на счёт Заказчика."),
    Item(0, "Гарантийные обязательства", True),
    Item(1, "Гарантийный срок на Товар составляет 60 (шестьдесят) месяцев с даты подписания "
            "документа о приёмке, но не менее срока гарантии производителя."),
    Item(1, "Размер обеспечения гарантийных обязательств составляет 10 (десять) процентов от "
            "начальной (максимальной) цены Контракта."),
    Item(0, "Ответственность сторон", True),
    Item(1, "В случае просрочки исполнения Поставщиком обязательств Поставщик уплачивает пеню в "
            "размере 1 (одного) процента от цены Контракта за каждый день просрочки."),
    Item(1, "За каждый факт неисполнения Поставщиком обязательств, не имеющего стоимостного "
            "выражения, Поставщик выплачивает штраф в размере 100 000 (ста тысяч) рублей."),
    Item(1, "В случае просрочки исполнения Заказчиком обязательств Поставщик вправе потребовать "
            "уплаты пени в размере одной трёхсотой действующей на дату уплаты пени ключевой ставки "
            "Центрального банка Российской Федерации от не уплаченной в срок суммы."),
    Item(0, "Расторжение контракта", True),
    Item(1, "Расторжение Контракта допускается по соглашению сторон, по решению суда или в связи с "
            "односторонним отказом стороны от исполнения Контракта."),
    Item(1, "Заказчик вправе принять решение об одностороннем отказе от исполнения Контракта в "
            "случае любого, в том числе однократного, нарушения Поставщиком условий Контракта, "
            "включая нарушение сроков устранения недостатков."),
    Item(0, "Заключительные положения", True),
    Item(1, "Контракт вступает в силу с даты его заключения и действует до 31 декабря 2026 года."),
    Item(1, "Все споры разрешаются в Арбитражном суде города Москвы."),
]

NOTICE_TITLE = "ИЗВЕЩЕНИЕ о проведении электронного аукциона № 0373100000126000045"

NOTICE: list[Item] = [
    Item(0, "Общие сведения о закупке", True),
    Item(1, "Способ определения поставщика: электронный аукцион."),
    Item(1, "Наименование объекта закупки: поставка серверного оборудования для центра обработки данных."),
    Item(1, "Начальная (максимальная) цена контракта: 4 850 000,00 рублей."),
    Item(0, "Требования к участникам закупки", True),
    Item(1, "Участник закупки должен иметь опыт исполнения не менее 5 (пяти) контрактов на поставку "
            "серверного оборудования за последние 3 года, цена каждого из которых составляет не менее "
            "50 процентов начальной (максимальной) цены контракта."),
    Item(1, "Участник закупки должен иметь лицензию ФСБ России на деятельность по разработке и "
            "производству шифровальных средств и статус авторизованного партнёра производителя."),
    Item(0, "Обеспечение заявки", True),
    Item(1, "Размер обеспечения заявки составляет 1 (один) процент начальной (максимальной) цены контракта."),
]

EXPECTED: list[dict] = [
    {"rule": "R-01", "doc": "contract", "quote": "сервер Lenovo ThinkSystem SR650 V3"},
    {"rule": "R-02", "doc": "contract", "quote": "Размер обеспечения исполнения Контракта составляет 30 (тридцать) процентов"},
    {"rule": "R-03", "doc": "contract", "quote": "пеню в размере 1 (одного) процента от цены Контракта за каждый день просрочки"},
    {"rule": "R-04", "doc": "contract", "quote": "в течение 3 (трёх) календарных дней с даты заключения Контракта"},
    {"rule": "R-05", "doc": "contract", "quote": "в случае любого, в том числе однократного, нарушения Поставщиком условий Контракта"},
    {"rule": "R-06", "doc": "notice", "quote": "опыт исполнения не менее 5 (пяти) контрактов"},
    {"rule": "R-06", "doc": "notice", "quote": "лицензию ФСБ России"},
    {"rule": "R-07", "doc": "contract", "quote": "в течение 30 (тридцати) рабочих дней с даты подписания Заказчиком документа о приёмке"},
    {"rule": "R-07", "doc": "contract", "quote": "в течение 25 (двадцати пяти) рабочих дней с даты поставки"},
    {"rule": "R-08", "doc": "contract", "quote": "Гарантийный срок на Товар составляет 60 (шестьдесят) месяцев"},
]

SUBJECT = "поставка серверного оборудования для центра обработки данных"

REQUISITES = (
    ["Заказчик:", "ФКУ «Центр обработки данных»", "ИНН 7701000001", "Директор ____ П. С. Иванов"],
    ["Поставщик:", "ООО «Северный терминал»", "ИНН 7802000002", "Генеральный директор ____ О. В. Смирнов"],
)


def expected_numbers(items: list[Item]) -> list[str | None]:
    """Номера пунктов так, как их нарисует Word для шаблона «%1.» / «%1.%2.»."""
    out: list[str | None] = []
    counters = [0, 0, 0]
    for item in items:
        if item.level < 0:
            out.append(None)
            continue
        counters[item.level] += 1
        for deeper in range(item.level + 1, 3):
            counters[deeper] = 0
        out.append(".".join(str(c) for c in counters[: item.level + 1]) + ".")
    return out


# --- DOCX ----------------------------------------------------------------------------

_LIST = lvl(0, "decimal", "%1.") + lvl(1, "decimal", "%1.%2.") + lvl(2, "decimal", "%1.%2.%3.")


def _docx_body(title: str, items: list[Item], requisites: bool) -> str:
    parts = [p(runs=r(title, "<w:b/>"), ppr='<w:jc w:val="center"/>')]
    for item in items:
        rpr = "<w:b/>" if item.bold else ""
        ppr = numpr(1, item.level) if item.level >= 0 else '<w:jc w:val="both"/>'
        if item.level == 0:
            ppr += '<w:keepNext/><w:jc w:val="center"/>'
        parts.append(p(runs=r(item.text, rpr), ppr=ppr))
    if requisites:
        cells = "".join(
            "<w:tc><w:tcPr><w:tcW w:w=\"4500\" w:type=\"dxa\"/></w:tcPr>" + "".join(p(t) for t in col) + "</w:tc>"
            for col in REQUISITES
        )
        parts.append(p(runs=r("Реквизиты и подписи сторон", "<w:b/>"), ppr=numpr(1, 0) + '<w:jc w:val="center"/>'))
        parts.append(f"<w:tbl><w:tblPr><w:tblW w:w=\"9000\" w:type=\"dxa\"/></w:tblPr><w:tr>{cells}</w:tr></w:tbl>")
    return "".join(parts)


def make_contract_docx(path: Path) -> Path:
    return make_docx(path, _docx_body(CONTRACT_TITLE, CONTRACT, True), numbering_xml=numbering({0: _LIST}, {1: 0}))


def make_notice_docx(path: Path) -> Path:
    return make_docx(path, _docx_body(NOTICE_TITLE, NOTICE, False), numbering_xml=numbering({0: _LIST}, {1: 0}))


# --- PDF своей вёрсткой ---------------------------------------------------------------


class _PdfLayout:
    """Простая вёрстка А4: поля 20/10/20/30 мм, 12 кегль, номер пункта с выступом,
    колонтитул и номер страницы, две колонки реквизитов, сноска."""

    def __init__(self, font_size: float = 12):
        regular = font_path("serif")
        bold = font_path("serif-bold") or regular
        if regular is None:
            raise RuntimeError("Нет шрифта с кириллицей: задайте BENCH_FONT_SERIF")
        self.font = pymupdf.Font(fontfile=str(regular))
        self.bold = pymupdf.Font(fontfile=str(bold))
        self.size = font_size
        self.doc = pymupdf.open()
        self.left, self.right = 85.0, 595.0 - 42.5
        self.top, self.bottom = 70.0, 842.0 - 70.0
        self.page = None
        self.y = 0.0
        self.footnotes: list[str] = []
        self._new_page()

    def _new_page(self) -> None:
        self._flush_footnotes()
        self.page = self.doc.new_page(width=595, height=842)
        no = self.doc.page_count
        tw = pymupdf.TextWriter(self.page.rect)
        tw.append((self.left, 40), "Проект контракта на поставку серверного оборудования", font=self.font, fontsize=9)
        tw.append((self.right - 60, 815), f"Стр. {no}", font=self.font, fontsize=9)
        tw.write_text(self.page)
        self.y = self.top

    def _flush_footnotes(self) -> None:
        if self.page is None or not self.footnotes:
            return
        tw = pymupdf.TextWriter(self.page.rect)
        y = self.bottom - 14 * len(self.footnotes)
        self.page.draw_line((self.left, y - 6), (self.left + 150, y - 6), width=0.5)
        for i, text in enumerate(self.footnotes, 1):
            tw.append((self.left, y + 4), str(i), font=self.font, fontsize=6)
            tw.append((self.left + 6, y + 8), text, font=self.font, fontsize=9)
            y += 14
        tw.write_text(self.page)
        self.footnotes = []

    def _wrap(self, text: str, font: pymupdf.Font, width: float, first_width: float) -> list[str]:
        lines, current = [], ""
        limit = first_width
        for word in text.split(" "):
            candidate = f"{current} {word}".strip()
            if font.text_length(candidate, fontsize=self.size) <= limit or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
                limit = width
        if current:
            lines.append(current)
        return lines

    def paragraph(self, text: str, *, number: str | None = None, bold: bool = False, center: bool = False,
                  indent: float = 0.0, space_after: float = 4.0, sup: str | None = None) -> None:
        font = self.bold if bold else self.font
        if center and number:
            text, number = f"{number} {text}", None
        lead = self.size * 1.2
        hang = 0.0
        if number:
            hang = font.text_length(number + " ", fontsize=self.size) + 2
        width = self.right - self.left - indent - hang
        first_width = width if number else width - 35
        lines = self._wrap(text, font, width, first_width if not center else width)
        for i, line in enumerate(lines):
            if self.y + lead > self.bottom - 14 * (len(self.footnotes) + 1):
                self._new_page()
            tw = pymupdf.TextWriter(self.page.rect)
            baseline = self.y + self.size
            if center:
                x = self.left + (self.right - self.left - font.text_length(line, fontsize=self.size)) / 2
            elif number:
                x = self.left + indent + hang
                if i == 0:
                    tw.append((self.left + indent, baseline), number, font=font, fontsize=self.size)
            else:
                x = self.left + indent + (35 if i == 0 else 0)
            tw.append((x, baseline), line, font=font, fontsize=self.size)
            if sup and i == len(lines) - 1:
                end = x + font.text_length(line, fontsize=self.size)
                tw.append((end + 0.5, baseline - self.size * 0.35), sup, font=font, fontsize=self.size * 0.6)
            tw.write_text(self.page)
            self.y += lead
        self.y += space_after

    def columns(self, cols: tuple[list[str], ...]) -> None:
        lead = self.size * 1.2
        if self.y + lead * max(len(c) for c in cols) > self.bottom:
            self._new_page()
        width = (self.right - self.left) / len(cols)
        tw = pymupdf.TextWriter(self.page.rect)
        for ci, col in enumerate(cols):
            for li, text in enumerate(col):
                tw.append((self.left + ci * width, self.y + self.size + li * lead), text, font=self.font, fontsize=self.size)
        tw.write_text(self.page)
        self.y += lead * max(len(c) for c in cols) + 6

    def save(self, path: Path) -> Path:
        self._flush_footnotes()
        self.doc.save(path, garbage=3, deflate=True)
        self.doc.close()
        return path


FILLER: list[Item] = [
    Item(0, "Порядок взаимодействия сторон", True),
    Item(1, "Стороны обмениваются документами по Контракту в единой информационной системе в сфере "
            "закупок, а при её недоступности — письмами на адреса, указанные в Контракте."),
    Item(1, "Сторона, изменившая адрес или банковские реквизиты, уведомляет об этом другую сторону "
            "в течение 5 (пяти) рабочих дней с даты изменения."),
    Item(1, "Документ считается полученным стороной в день его размещения в единой информационной "
            "системе или в день вручения письма."),
    Item(1, "Представители сторон, ответственные за исполнение Контракта, назначаются приказами "
            "сторон и указываются в Приложении № 3 к Контракту."),
]


def long_contract(pages: int) -> list[Item]:
    """Договор нужной длины для замера скорости: основные разделы и повторяющиеся служебные."""
    items = list(CONTRACT)
    while len(items) < pages * 14:
        items.extend(FILLER)
    return items


def make_contract_pdf(path: Path, *, font_size: float = 12, items: list[Item] | None = None) -> Path:
    items = items or CONTRACT
    layout = _PdfLayout(font_size)
    layout.paragraph(CONTRACT_TITLE, bold=True, center=True, space_after=10)
    numbers = expected_numbers(items)
    for item, number in zip(items, numbers, strict=True):
        sup = None
        if item.text.startswith("Цена Контракта составляет"):
            sup = "1"
            layout.footnotes.append("Цена указана с учётом всех налогов и сборов.")
        layout.paragraph(item.text, number=number, bold=item.bold, center=item.level == 0, sup=sup,
                         space_after=8 if item.level == 0 else 4)
    layout.paragraph("Реквизиты и подписи сторон", number=f"{len([i for i in items if i.level == 0]) + 1}.",
                     bold=True, center=False)
    layout.columns(REQUISITES)
    return layout.save(path)


def make_notice_pdf(path: Path) -> Path:
    layout = _PdfLayout()
    layout.paragraph(NOTICE_TITLE, bold=True, center=True, space_after=10)
    for item, number in zip(NOTICE, expected_numbers(NOTICE), strict=True):
        layout.paragraph(item.text, number=number, bold=item.bold)
    return layout.save(path)
