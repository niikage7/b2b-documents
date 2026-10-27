"""Стенд паспорта закупки: попало ли значение каждого поля в запрос к LLM.

Паспорт не заполнит поле, если нужного текста нет во фрагментах запроса, поэтому поиск
меряется отдельно и бесплатно: для каждого поля из разметки `passport.json` проверяется,
есть ли цитата со значением во фрагментах того документа, где она стоит. С `--llm` паспорт
заполняется по-настоящему и значения сверяются с разметкой (около $0,001–0,002 на пакет).

    python -m bench.passport                                   # три открытых пакета
    python -m bench.passport --budget 400,800 --show customer
    python -m bench.passport --llm

Разметка — `bench/packages/<номер>/passport.json`: поле → список вариантов {doc, quote};
поле засчитывается, если найден любой вариант. Поля, значения которых в документах нет
(номер ЕИС у заказчика, который его не печатает), в разметку не входят.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from ml.analysis.anchor import normalize_map
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks, estimate_tokens
from ml.analysis.embeddings import default_embedder
from ml.analysis.passport import FIELD_BUDGET_TOKENS, FIELDS, extract_passport, select_fragments
from ml.analysis.search import InMemorySearch
from ml.extract import extract_document

PACKAGES = Path(__file__).parent / "packages"
FILES = Path(".cache/packages")


def _norm(text: str) -> str:
    return normalize_map(text)[0]


def load_package(number: str) -> list[tuple[str, str, list]]:
    docs = []
    for path in sorted((FILES / number).iterdir()):
        if path.suffix.lower() in (".docx", ".doc", ".pdf", ".rtf"):
            docs.append((path.stem, path.name, blocks_from_extraction(extract_document(path))))
    return docs


def coverage(fragments, expected: dict[str, list[dict]]) -> dict[str, bool]:
    texts: dict[str, str] = defaultdict(str)
    for f in fragments:
        texts[f.document_id] += "\n" + _norm(f.text)
    return {key: any(_norm(v["quote"]) in texts[v["doc"]] for v in variants) for key, variants in expected.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("packages", nargs="*", help="номера закупок; по умолчанию все с passport.json")
    parser.add_argument("--budget", default=str(FIELD_BUDGET_TOKENS), help="бюджеты поля через запятую")
    parser.add_argument("--show", action="append", default=[], help="напечатать фрагменты поля")
    parser.add_argument("--llm", action="store_true", help="заполнить паспорт и сверить значения")
    args = parser.parse_args()

    numbers = args.packages or sorted(p.name for p in PACKAGES.iterdir() if (p / "passport.json").exists())
    budgets = [int(x) for x in args.budget.split(",")]
    embedder = default_embedder()
    misses: dict[int, list[str]] = defaultdict(list)
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    for number in numbers:
        expected = json.loads((PACKAGES / number / "passport.json").read_text())
        docs = load_package(number)
        search = InMemorySearch()
        for doc_id, _, blocks in docs:
            search.add(doc_id, chunk_blocks(sorted(blocks, key=lambda b: b.order), prefix=f"{doc_id}:"),
                       embedder=embedder)
        for budget in budgets:
            fragments = select_fragments(docs, search=search, embedder=embedder, budget_tokens=budget)
            found = coverage(fragments, expected)
            tokens = sum(estimate_tokens(f.text) for f in fragments)
            totals[budget][0] += sum(found.values())
            totals[budget][1] += len(found)
            totals[budget][2] += tokens
            misses[budget] += [f"{number[-4:]}:{key}" for key, ok in found.items() if not ok]
            print(f"{number} бюджет {budget}: {sum(found.values())} из {len(found)}, фрагментов {len(fragments)}, "
                  f"≈{tokens} токенов")
            for key in args.show:
                specs = [f for f in FIELDS if f.key == key]
                if not specs:
                    continue
                for f in select_fragments(docs, search=search, embedder=embedder, fields=specs, budget_tokens=budget):
                    print(f"  [{key}] {f.filename}: {f.text[:300]!r}")
        if args.llm:
            passport = extract_passport(docs, search=search, embedder=embedder, budget_tokens=budgets[-1])
            values = {f.key: f for f in passport.fields}
            right = 0
            for key, variants in expected.items():
                got = values.get(key)
                ok = got is not None and any(_norm(v["quote"]) in _norm(got.quote) or _norm(got.quote) in _norm(v["quote"])
                                             for v in variants)
                right += ok
                print(f"  {key:<22} {'OK ' if ok else '—  '} {got.value if got else ''}")
            print(f"  верно {right} из {len(expected)}, ${passport.usage.cost:.4f}")

    print(f"\n{'бюджет':>7}{'найдено':>10}{'доля':>7}{'токенов':>9}  пропуски")
    for budget in budgets:
        ok, n, tokens = totals[budget]
        print(f"{budget:>7}{ok:>7} из {n:<3}{ok / n:>5.2f}{tokens // len(numbers):>9}  {', '.join(misses[budget]) or '—'}")


if __name__ == "__main__":
    main()
