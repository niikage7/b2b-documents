"""Исправление типичных ошибок Tesseract `rus+eng` по самому слову и его соседям в строке.

Ошибки найдены стендом `bench.ocr_cer` на сканах из макетов настоящих документов
(docs/ml-plan.md). Каждое правило узкое: слово меняется, только если ошибка видна из него
самого или из соседних слов строки.

- Римский номер раздела в начале строки: «ГУ. Взаимодействие Сторон» → «IV.», «УП.» → «VII.».
  Без этого шаблон номера пункта его не узнаёт, и у раздела скана нет номера.
- Подпункт «6)» после подпункта «а)» — это «б)», «3)» после «ж)» — «з)».
- Слово из кириллицы и латиницы вперемешку («Tpeбования») — буквы меньшинства переводятся
  в алфавит большинства. Короткое слово целиком из латинских двойников между русскими
  словами («Ha», «OT», «Tun») — в кириллицу.
- Цифры 6, 0 и 3 внутри русского слова: «6ыть» → «быть», «py6.» → «руб.», «3аказчик» → «Заказчик».
- «М» перед номером — латинская «N», которой в документах пишут знак номера: «М 44-ФЗ» → «N 44-ФЗ».
- «44-03», «44-®3» → «44-ФЗ».
"""

from __future__ import annotations

import re

from ml.ocr.lines import pixel_lines
from ml.ocr.tesseract import TsvWord

_LAT_TWINS = "ABCEHKMOPTXaceopxyun"
_CYR_TWINS = "АВСЕНКМОРТХасеорхуип"
_TO_CYR = dict(zip(_LAT_TWINS, _CYR_TWINS, strict=True))
_TO_LAT = {c: lat for lat, c in zip(_LAT_TWINS[:-2], _CYR_TWINS[:-2], strict=True)}
"""«u» и «n» — не двойники «и» и «п», но Tesseract путает их так же часто; обратно не переводятся."""

_CYR = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT = re.compile(r"[a-z]", re.IGNORECASE)

_ROMAN_TWINS = {"Г": "I", "Т": "I", "І": "I", "[": "I", "|": "I", "l": "I", "У": "V", "Х": "X", "Ш": "III", "П": "II"}
_ROMAN_TOKEN = re.compile(r"^([IVXГТІ\[|lУХШП]{1,6})\.$")
_ROMAN = re.compile(r"^(?:X{0,3})(?:IX|IV|V?I{0,3})$")
_MARKERS = {"6)": ("б)", {"а)", "a)"}), "3)": ("з)", {"ж)"})}
_LAW = re.compile(r"^(\d{2,3})-(?:03|0З|О3|ОЗ|O3|®3|D3|Ф3)(\W*)$")
_DIGIT_TWINS = {"6": "б", "0": "о"}


def _letters(text: str) -> tuple[int, int]:
    return len(_CYR.findall(text)), len(_LAT.findall(text))


def _script(word: TsvWord | None) -> str | None:
    if word is None:
        return None
    cyr, lat = _letters(word.text)
    if cyr and not lat:
        return "cyr"
    if lat and not cyr:
        return "lat"
    return None


def _roman(text: str, following: TsvWord | None) -> str | None:
    """Номер раздела из двойников: только с точкой, в начале строки и перед словом с прописной."""
    m = _ROMAN_TOKEN.match(text)
    if not m or following is None or not following.text[:1].isupper():
        return None
    value = "".join(_ROMAN_TWINS.get(ch, ch) for ch in m.group(1))
    if value == m.group(1) or not _ROMAN.match(value):
        return None
    # Одиночные «Г.» и «Т.» — чаще сокращения («Г. МОСКВА»), чем номер I; «1» не берём вовсе: «11.» — номер пункта
    if m.group(1) in ("Г", "Т", "[", "|", "l"):
        return None
    return value + "."


def _mixed(text: str) -> str:
    """Буквы меньшинства — в алфавит большинства, если у всех есть двойник; при равенстве — кириллица."""
    cyr, lat = _letters(text)
    if not (cyr and lat):
        return text
    table = _TO_CYR if cyr >= lat else _TO_LAT
    minority = _LAT if cyr >= lat else _CYR
    if not all(ch in table for ch in minority.findall(text)):
        return text
    return "".join(table.get(ch, ch) if minority.match(ch) else ch for ch in text)


def _twins(text: str, prev: str | None, nxt: str | None) -> str:
    """Короткое слово из латинских двойников между русскими словами — в кириллицу: «Ha», «OT»,
    «py6.». Слова с другими цифрами («M.2», «A4») — обычно латинские коды, их не трогаем.
    Обратного правила нет: русские сокращения среди латиницы («ОКАТО» в реквизитах)
    встречаются чаще ошибок."""
    cyr, lat = _letters(text)
    neighbours = {s for s in (prev, nxt) if s is not None}
    if not lat or cyr or lat > 5 or neighbours != {"cyr"} or any(ch.isdigit() and ch not in "60" for ch in text):
        return text
    if not all(ch in _TO_CYR for ch in _LAT.findall(text)):
        return text
    return "".join(_TO_CYR.get(ch, ch) for ch in text)


def _digits(text: str) -> str:
    """6 и 0 рядом с русской буквой → «б», «о»; 3 в начале перед строчными → «З». Только в
    слове, где русских букв не меньше двух и цифр меньше, чем букв: «1920х1080» не трогаем."""
    cyr, lat = _letters(text)
    digits = sum(ch.isdigit() for ch in text)
    if lat or cyr < 2 or digits == 0 or digits >= cyr or re.search(r"\d\d", text):
        return text
    chars = list(text)
    for i, ch in enumerate(chars):
        before = chars[i - 1] if i else ""
        after = chars[i + 1] if i + 1 < len(chars) else ""
        if ch in _DIGIT_TWINS and (_CYR.match(before) or _CYR.match(after)):
            chars[i] = _DIGIT_TWINS[ch]
        elif ch == "3" and i == 0 and re.match(r"[а-яё]{2}", text[1:3]):
            chars[i] = "З"
    return "".join(chars)


def correct_words(words: list[TsvWord]) -> int:
    """Исправляет слова на месте. Возвращает число изменённых слов."""
    changed = 0
    last_marker: str | None = None
    for line in pixel_lines(words):
        for i, w in enumerate(line):
            prev = line[i - 1] if i else None
            nxt = line[i + 1] if i + 1 < len(line) else None
            text = w.text
            roman = _roman(text, nxt) if i == 0 else None
            if i == 0:
                marker = _MARKERS.get(text)
                if marker and last_marker in marker[1]:
                    text = marker[0]
                if re.match(r"^(?:[а-яёa-z]|\d{1,2})\)$", text):
                    last_marker = text
            if roman:
                # Номер раздела — латиницей, правило двойников вернуло бы «X.» в кириллицу
                text = roman
            else:
                text = _digits(_twins(_mixed(text), _script(prev), _script(nxt)))
            if text == "М" and nxt is not None and nxt.text[:1].isdigit():
                text = "N"
            if m := _LAW.match(text):
                text = f"{m.group(1)}-ФЗ{m.group(2)}"
            if text != w.text:
                w.text = text
                changed += 1
    return changed
