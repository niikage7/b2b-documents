"""Вопросы к пакету: нашёлся ли ответ там, где он размечен, и молчит ли модель, когда ответа нет.

Вопросы — поля паспорта закупки, эталон — `bench/packages/<номер>/passport.json`: ответ засчитывается, если
цитата ответа совпала с размеченной. Вопросы-ловушки спрашивают о том, чего в документах поставки нет:
правильный ответ — «в документах ответа нет». Около $0,0007 на вопрос.

    python -m bench.ask --package 0373400006826000073
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench.pipeline import quotes_match
from contracts.analysis import LLMUsage
from ml.analysis.ask import ask_package
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks
from ml.analysis.embeddings import default_embedder
from ml.analysis.search import InMemorySearch
from ml.convert import convert_to_docx
from ml.extract import extract_document

QUESTIONS = {
    "subject": "Что закупается?", "customer": "Кто заказчик?", "nmck": "Какая начальная цена контракта?",
    "application_security": "Сколько составляет обеспечение заявки?",
    "contract_security": "Какой размер обеспечения исполнения контракта?",
    "delivery_term": "В какой срок надо поставить товар?", "delivery_place": "Куда доставлять товар?",
    "acceptance_term": "Сколько времени заказчик принимает товар?", "payment_term": "Когда заказчик оплатит товар?",
    "warranty_term": "Какой гарантийный срок на товар?", "deadline": "Когда заканчивается подача заявок?",
    "advance": "Предусмотрен ли аванс?",
}
ABSENT = ("Нужно ли проводить пусконаладку на космической станции?", "Какой курс биткоина указан в контракте?",
          "Кто директор поставщика по имени Иннокентий?", "Какой номер лицензии ФСБ у заказчика?")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", required=True)
    args = parser.parse_args()
    package = Path(".cache/packages") / args.package
    embedder = default_embedder()
    store = InMemorySearch()
    documents = []
    for src in sorted(package.iterdir()):
        path = Path(convert_to_docx(src)) if src.suffix.lower() == ".doc" else src
        blocks = blocks_from_extraction(extract_document(path))
        documents.append((src.stem, src.name, blocks))
        store.add(src.stem, chunk_blocks(blocks), embedder=embedder)
    marked = json.loads((Path("bench/packages") / args.package / "passport.json").read_text())
    usage = LLMUsage()
    hits = answered = total = 0
    for key, question in QUESTIONS.items():
        if key not in marked:
            continue
        total += 1
        result = ask_package(question, documents, search=store, embedder=embedder)
        usage.add(result.usage)
        answered += result.found
        # Значение то же, что в разметке: модель могла взять его из другого документа пакета
        ok = result.found and any(
            quotes_match(m["quote"], c.quote) for m in marked[key] for c in result.citations)
        hits += ok
        print(f"{'да ' if ok else 'нет'} {question}\n    {result.answer[:160]}")
        if result.found and not ok:
            print(f"    цитата: {result.citations[0].quote[:120]}\n    эталон: {marked[key][0]['quote'][:120]}")
    silent = 0
    for question in ABSENT:
        result = ask_package(question, documents, search=store, embedder=embedder)
        usage.add(result.usage)
        silent += not result.found
        print(f"{'молчит' if not result.found else 'ОТВЕТИЛА'} {question}\n    {result.answer[:160]}")
    print(f"\nЦитата совпала с разметкой: {hits} из {total}, ответ дан: {answered} из {total}; "
          f"ловушки без ответа: {silent} из {len(ABSENT)}; ${usage.cost:.4f}")


if __name__ == "__main__":
    main()
