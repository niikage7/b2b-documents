"""Чистка текста абзацев без сдвига смещений и номер пункта, набранный в начале текста."""

from __future__ import annotations

import re

from pydantic import BaseModel

CLAUSE_NUMBER = re.compile(
    r"""^(?:
        (?:Раздел|РАЗДЕЛ|Статья|СТАТЬЯ|Глава|ГЛАВА|Часть|ЧАСТЬ|Приложение|ПРИЛОЖЕНИЕ)\s+(?:№\s*)?[\dIVXLC]{1,6}\.?
      | \d{1,3}(?:\.\d{1,3}){0,5}\.(?!\d)
      | \d{1,3}(?:\.\d{1,3}){1,5}(?![\d.])
      | \d{1,3}(?:\.\d{1,3}){0,5}\)
      | \(\d{1,3}\)
      | [а-яёa-z]\)
      | [IVXLC]{1,6}\.
    )(?=\s+\S)""",
    re.VERBOSE,
)
"""Номер пункта, набранный в начале текста: «6.4.», «IV.», «а)», «Раздел 2». Для PDF и для DOCX без автонумерации."""


def split_clause_number(text: str) -> tuple[str, int] | None:
    """Номер в начале текста и позиция, с которой начинается текст пункта без номера и пробелов."""
    m = CLAUSE_NUMBER.match(text)
    if not m or len(text) <= m.end():
        return None
    rest = text[m.end():]
    return m.group(0), m.end() + len(rest) - len(rest.lstrip())


def clean_text(text: str) -> str:
    """Заменяет символ на символ, длина строки не меняется.

    Символы вне BMP → U+FFFD (смещения Python и JavaScript совпадают), управляющие
    символы, кроме табуляции и перевода строки, → пробел.
    """
    if text.isascii() and text.isprintable():
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            out.append("�")
        elif code < 0x20 and ch not in "\t\n":
            out.append(" ")
        elif 0x7F <= code < 0xA0:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def drop_prefix[S: BaseModel](text: str, spans: list[S], cut: int) -> tuple[str, list[S]]:
    """Убирает первые `cut` знаков текста и пересчитывает отрезки (`start`, `end`, `run_offset`)."""
    out: list[S] = []
    for span in spans:
        if span.end <= cut:
            continue
        s = max(span.start, cut)
        update = {"start": s - cut, "end": span.end - cut}
        if hasattr(span, "run_offset"):
            update["run_offset"] = span.run_offset + (s - span.start)
        out.append(span.model_copy(update=update))
    return text[cut:], out


def strip_spans[S: BaseModel](text: str, spans: list[S]) -> tuple[str, list[S]]:
    """Обрезает пробелы по краям текста и пересчитывает отрезки (`start`, `end`, `run_offset`)."""
    lead = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if lead == 0 and end == len(text):
        return text, spans
    out: list[S] = []
    for span in spans:
        s = max(span.start, lead)
        e = min(span.end, end)
        if s >= e:
            continue
        update = {"start": s - lead, "end": e - lead}
        if hasattr(span, "run_offset"):
            update["run_offset"] = span.run_offset + (s - span.start)
        out.append(span.model_copy(update=update))
    return text[lead:end], out
