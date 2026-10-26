"""Чистка текста абзацев без сдвига смещений."""

from __future__ import annotations

from pydantic import BaseModel


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
