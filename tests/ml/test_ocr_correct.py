from __future__ import annotations

import numpy as np
import pytest

import ml.ocr.lines as lines_mod
from ml.ocr.correct import correct_words, restore_one
from ml.ocr.lines import recover_missed_text
from ml.ocr.tesseract import TsvWord


def _line(*texts: str, y: int = 100) -> list[TsvWord]:
    return [TsvWord(1, 1, 1, 100 * i, y, 80, 30, 0.9, t) for i, t in enumerate(texts)]


def _fix(*texts: str) -> str:
    words = _line(*texts)
    correct_words(words)
    return " ".join(w.text for w in words)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (("ГУ.", "Взаимодействие", "Сторон"), "IV. Взаимодействие Сторон"),
        (("УП.", "Обеспечение"), "VII. Обеспечение"),
        (("Х.", "Обстоятельства", "силы"), "X. Обстоятельства силы"),
        (("[Х.", "Обстоятельства"), "IX. Обстоятельства"),
        (("П.", "Цена", "Контракта"), "II. Цена Контракта"),
        # Не номер: сокращение, номер внутри строки, номер перед строчной, номер пункта
        (("Г.", "МОСКВА"), "Г. МОСКВА"),
        (("ГУ", "Банка", "России"), "ГУ Банка России"),
        (("П.", "3.2", "Контракта"), "П. 3.2 Контракта"),
        (("11.", "Срок", "действия"), "11. Срок действия"),
        (("в", "Ha", "основании"), "в На основании"),
        (("Tun", "стола"), "Тип стола"),
        (("Tpeбования", "к", "участникам"), "Требования к участникам"),
        (("1000", "py6.", "за"), "1000 руб. за"),
        (("3аказчик", "обязан"), "Заказчик обязан"),
        (("закона", "М", "44-03;"), "закона N 44-ФЗ;"),
        # Латинские коды и числа не трогаем
        (("разъём", "M.2", "для"), "разъём M.2 для"),
        (("USB", "Type-C", "на"), "USB Type-C на"),
        (("1920х1080", "пикселей"), "1920х1080 пикселей"),
        (("10", "x", "20"), "10 x 20"),
        (("код", "ОКАТО", "e-mail"), "код ОКАТО e-mail"),
    ],
)
def test_correct_words(before, after):
    assert _fix(*before) == after


def test_one_before_numeral_in_brackets():
    words = _line("в", "течение", "|", "(одного)", "рабочего", "дня", "|", "(одна)", "l", "(один)", "|", "день")
    assert restore_one(words) == 3
    assert " ".join(w.text for w in words) == "в течение 1 (одного) рабочего дня 1 (одна) 1 (один) | день"
    # Черта в конце строки Tesseract и числительное на следующей — не трогаем
    tail = _line("течение", "|") + [TsvWord(1, 1, 2, 0, 140, 80, 30, 0.9, "(одного)")]
    assert restore_one(tail) == 0


def test_sub_item_markers_follow_previous_marker():
    words = _line("а)", "первое", y=100) + _line("6)", "второе", y=200)
    correct_words(words)
    assert [w.text for w in words] == ["а)", "первое", "б)", "второе"]
    words = _line("5)", "пятое", y=100) + _line("6)", "шестое", y=200)
    correct_words(words)
    assert [w.text for w in words][2] == "6)"


def test_recover_missed_text_reads_isolated_values(monkeypatch):
    """Одиночное значение в колонке дочитывается в координатах страницы; хвост буквы рядом со
    словом и пятно выше двух строк (подпись, печать) — нет."""
    img = np.full((600, 1200), 255, np.uint8)
    words = [TsvWord(1, 1, 1, 100, 100, 200, 30, 0.95, "Количество"),
             TsvWord(1, 1, 2, 100, 300, 200, 30, 0.95, "портов")]
    img[105:125, 320:326] = 0     # хвост буквы в 20 px от слова — закрыт полем вокруг слова
    img[100:128, 800:840] = 0     # значение «≥ 4» в соседней колонке
    img[300:500, 600:700] = 0     # подпись: выше двух строк
    sheets = []

    def fake_recognize(sheet, **_):
        sheets.append(sheet.shape)
        return [TsvWord(1, 1, 1, 24 + 2, 24 + 4, 36, 24, 0.9, ">4")]

    monkeypatch.setattr(lines_mod, "recognize", fake_recognize)
    assert recover_missed_text(img, words, lang="rus+eng", dpi=300) == 1
    added = words[-1]
    assert added.text == ">4"
    assert (added.left, added.top) == (800 - lines_mod.PAD + 2, 100 - lines_mod.PAD + 4)
    assert added.block == 2
    assert len(sheets) == 1
