"""Проверки пакета без LLM: цифры против прописи, контрольные суммы реквизитов, процент от НМЦК.

Опечатка в сумме или сроке — готовый риск: при расхождении цифр и прописи стороны спорят, какое значение
верно, неверный ИНН заказчика ломает платёж и банковскую гарантию. Такие вещи считаются точно и за
миллисекунды, цитата и раздел у находки те же, что у остальных.

Принцип один: лучше промолчать, чем обвинить зря. Пропись с незнакомым словом, дробь или порядковое
число не оцениваются; предупреждение просит сверить значение, а не утверждает ошибку.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from contracts.analysis import PackageFinding
from ml.analysis.blocks import BlockLike, section_of

CODE_NUMBER_WORDS = "A-01"
CODE_REQUISITES = "A-02"
CODE_PERCENT = "A-03"
TITLES = {
    CODE_NUMBER_WORDS: "Число цифрами не совпадает с прописью",
    CODE_REQUISITES: "Неверная контрольная сумма реквизита",
    CODE_PERCENT: "Сумма не равна указанному проценту от начальной цены",
}

_UNITS = {
    0: "ноль нуля нолю нолем нуль",
    1: "один одна одно одного одной одному одну одним одном",
    2: "два две двух двум двумя",
    3: "три трех трех трем тремя",
    4: "четыре четырех четырем четырьмя",
    5: "пять пяти пятью",
    6: "шесть шести шестью",
    7: "семь семи семью",
    8: "восемь восьми восемью",
    9: "девять девяти девятью",
    10: "десять десяти десятью",
    11: "одиннадцать одиннадцати",
    12: "двенадцать двенадцати",
    13: "тринадцать тринадцати",
    14: "четырнадцать четырнадцати",
    15: "пятнадцать пятнадцати",
    16: "шестнадцать шестнадцати",
    17: "семнадцать семнадцати",
    18: "восемнадцать восемнадцати",
    19: "девятнадцать девятнадцати",
    20: "двадцать двадцати",
    30: "тридцать тридцати",
    40: "сорок сорока",
    50: "пятьдесят пятидесяти",
    60: "шестьдесят шестидесяти",
    70: "семьдесят семидесяти",
    80: "восемьдесят восьмидесяти",
    90: "девяносто девяноста",
    100: "сто ста",
    200: "двести двухсот двумстам",
    300: "триста трехсот тремстам",
    400: "четыреста четырехсот четыремстам",
    500: "пятьсот пятисот пятистам",
    600: "шестьсот шестисот шестистам",
    700: "семьсот семисот семистам",
    800: "восемьсот восьмисот восьмистам",
    900: "девятьсот девятисот девятистам",
}
_WORDS = {form: value for value, forms in _UNITS.items() for form in forms.split()}
_SCALES = {
    **dict.fromkeys("тысяча тысячи тысячу тысяч тысячам тысячах тысячей".split(), 1_000),
    **dict.fromkeys("миллион миллиона миллиону миллионов миллионам миллионах".split(), 1_000_000),
    **dict.fromkeys("миллиард миллиарда миллиарду миллиардов миллиардам".split(), 1_000_000_000),
}
# Единицы внутри скобок: «(пять тысяч) рублей» и «(пять тысяч рублей 00 копеек)» читаются одинаково
_UNIT_TAIL = re.compile(r"\s+(?:руб|коп|процент|дн|день|дня|месяц|календарн|рабоч|год|лет|недел|%|\d).*$", re.I)
_NUMBER_WITH_WORDS = re.compile(
    r"(?<![\d./№-])(\d{1,3}(?:[\s ]\d{3})+|\d+)([.,]\d{1,4})?\s*(?:руб\w*\.?\s*)?\(\s*([а-яёА-ЯЁ][а-яёА-ЯЁ\s]{1,200}?(?:\s+\d[^)]*)?)\s*\)"
)


def parse_number_words(text: str) -> int | None:
    """«семи» → 7, «сто двадцать тысяч три» → 120003. Незнакомое слово или неверный порядок → None."""
    words = _UNIT_TAIL.sub("", text.lower().replace("ё", "е")).split()
    if not words:
        return None
    total = 0
    part = 0
    last_scale = 10**12
    last_value = 10**4
    for word in words:
        if word in _SCALES:
            scale = _SCALES[word]
            if scale >= last_scale:
                return None
            total += (part or 1) * scale
            part = 0
            last_scale = scale
            last_value = 10**4
        elif word in _WORDS:
            value = _WORDS[word]
            # Разряды идут по убыванию: «сто двадцать три»; «двадцать сто» — это не число
            if value >= last_value or (last_value < 20 and value < 20):
                return None
            part += value
            last_value = value
        else:
            return None
    return total + part


def _digits(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def _finding(code: str, block: BlockLike, document_id: str, filename: str | None, start: int, end: int,
             explanation: str, recommendation: str, *, level: str = "warning") -> PackageFinding:
    return PackageFinding(
        code=code, title=TITLES[code], level=level, document_id=document_id, filename=filename, block_id=block.id,
        start=start, end=end, quote=block.text[start:end], explanation=explanation, recommendation=recommendation,
        confidence=1.0, page=getattr(block, "page", None), section_number=section_of(block),
    )


def check_number_words(document_id: str, filename: str | None, blocks: Sequence[BlockLike]) -> list[PackageFinding]:
    """«10 (пятнадцать) дней», «1 500 000 (один миллион) рублей»."""
    out = []
    for block in blocks:
        for m in _NUMBER_WITH_WORDS.finditer(block.text):
            words = parse_number_words(m.group(3))
            if words is None:
                continue
            number = _digits(m.group(1))
            if number == words:
                continue
            out.append(_finding(
                CODE_NUMBER_WORDS, block, document_id, filename, m.start(), m.end(),
                f"Цифрами указано {m.group(1)}, прописью — {words}: при споре стороны могут опираться на разные "
                "значения.",
                "Направить заказчику запрос на разъяснение и попросить указать одно значение.",
            ))
    return out


_INN = re.compile(r"\bИНН\s*[:№]?\s*(\d{10}|\d{12})\b")
_OGRN = re.compile(r"\bОГРН(?:ИП)?\s*[:№]?\s*(\d{13}|\d{15})\b")


def inn_valid(inn: str) -> bool:
    digits = [int(c) for c in inn]

    def control(weights: tuple[int, ...]) -> int:
        return sum(w * d for w, d in zip(weights, digits, strict=False)) % 11 % 10

    if len(digits) == 10:
        return control((2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[9]
    if len(digits) == 12:
        return (control((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[10]
                and control((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[11])
    return False


def ogrn_valid(ogrn: str) -> bool:
    if len(ogrn) == 13:
        return int(ogrn[:12]) % 11 % 10 == int(ogrn[12])
    if len(ogrn) == 15:
        return int(ogrn[:14]) % 13 % 10 == int(ogrn[14])
    return False


def _placeholder(value: str) -> bool:
    """«0000000000», «1234567890» — незаполненное поле формы, а не реквизит."""
    return len(set(value)) == 1 or value in "1234567890123456"


def check_requisites(document_id: str, filename: str | None, blocks: Sequence[BlockLike]) -> list[PackageFinding]:
    out = []
    seen: set[str] = set()
    for block in blocks:
        for pattern, name, valid in ((_INN, "ИНН", inn_valid), (_OGRN, "ОГРН", ogrn_valid)):
            for m in pattern.finditer(block.text):
                value = m.group(1)
                if value in seen or _placeholder(value) or valid(value):
                    continue
                seen.add(value)
                out.append(_finding(
                    CODE_REQUISITES, block, document_id, filename, m.start(), m.end(),
                    f"{name} {value} не проходит проверку контрольной суммы: в реквизите, вероятно, опечатка. По "
                    "неверным реквизитам не пройдут платёж и банковская гарантия.",
                    f"Сверить {name} с реестром и при расхождении направить заказчику запрос на разъяснение.",
                ))
    return out


_MONEY = r"(\d{1,3}(?:[\s ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_NMCK = re.compile(
    r"(?:начальн\w*\s*\(максимальн\w*\)\s*цен\w*(?:\s+контракта|\s+договора)?|НМЦК|НМЦД)[^0-9%]{0,80}?" + _MONEY
    + r"\s*(?:руб|₽|российск)", re.IGNORECASE)
_PERCENT_SUM = re.compile(
    r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:\([^)]{1,40}\)\s*)?(?:%|процент\w*)\s*(?:от\s+)?(?:начальн\w*\s*\(максимальн\w*\)\s*цен\w*"
    r"(?:\s+контракта|\s+договора)?|НМЦК|НМЦД)[^0-9%]{0,80}?" + _MONEY + r"\s*(?:руб|₽|российск)", re.IGNORECASE)


def _money(text: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[\s ]", "", text).replace(",", "."))
    except InvalidOperation:
        return None


def find_nmck(documents: Sequence[tuple[str, str | None, Sequence[BlockLike]]]) -> Decimal | None:
    """Начальная цена из текста пакета; разные значения в пакете не сравниваются здесь — это X-01."""
    for _, _, blocks in documents:
        for block in blocks:
            if _PERCENT_SUM.search(block.text):
                continue  # «5 % от начальной цены … 270 995 руб.» — сумма обеспечения, а не цена
            m = _NMCK.search(block.text)
            value = _money(m.group(1)) if m else None
            if value and value > 0:
                return value
    return None


def check_percent_sums(documents: Sequence[tuple[str, str | None, Sequence[BlockLike]]],
                       nmck: Decimal | None = None) -> list[PackageFinding]:
    """«5 % от начальной цены, что составляет 270 995,15 руб.» при НМЦК 5 419 903,00 руб."""
    nmck = nmck or find_nmck(documents)
    if not nmck:
        return []
    out = []
    for document_id, filename, blocks in documents:
        for block in blocks:
            for m in _PERCENT_SUM.finditer(block.text):
                percent, amount = _money(m.group(1)), _money(m.group(2))
                if percent is None or amount is None:
                    continue
                expected = nmck * percent / 100
                # Рубль на округление и 1 %: сумму могут считать от НМЦК с другими копейками
                if abs(expected - amount) <= max(Decimal(1), expected / 100):
                    continue
                out.append(_finding(
                    CODE_PERCENT, block, document_id, filename, m.start(), m.end(),
                    f"{m.group(1)} % от начальной цены {nmck:,.2f} руб. — это {expected:,.2f} руб., а в условии указано "
                    f"{m.group(2)} руб.".replace(",", " "),
                    "Направить запрос на разъяснение: какая сумма обеспечения верна.",
                ))
    return out


def run_checks(documents: Sequence[tuple[str, str | None, Sequence[BlockLike]]]) -> list[PackageFinding]:
    """Все проверки без LLM по документам пакета: [(document_id, имя файла, блоки)]."""
    out: list[PackageFinding] = []
    for document_id, filename, blocks in documents:
        out += check_number_words(document_id, filename, blocks)
        out += check_requisites(document_id, filename, blocks)
    return out + check_percent_sums(documents)
