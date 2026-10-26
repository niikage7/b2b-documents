"""Стенд выбора bi-encoder: находит ли строка поиска правила блок с ожидаемой цитатой.

Для каждой ожидаемой находки (`bench.samples.EXPECTED` или свой JSON) запрос — строка
поиска правила («название + описание»), кандидаты — блоки того же документа. Метрики —
recall@k (есть ли нужный блок в первых k) и MRR, плюс скорость кодирования на CPU.

    python -m bench.embeddings intfloat/multilingual-e5-base deepvk/USER-bge-m3 sergeyzh/BERTA

Свой эталон: `--expected file.json` со списком {"rule", "doc", "quote"} и `--doc имя=путь`.
Модели скачиваются с Hugging Face при первом запуске.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from ml.analysis.anchor import normalize_map
from ml.analysis.blocks import blocks_from_extraction
from ml.analysis.embeddings import SentenceTransformerEmbedder
from ml.analysis.templates import rule_query_text
from ml.checklist import load_default_checklist
from ml.extract import extract_document

KS = (1, 3, 5, 8)


def _contains(block_text: str, quote: str) -> bool:
    return normalize_map(quote)[0] in normalize_map(block_text)[0]


def evaluate(model: str, docs: dict[str, list], expected: list[dict], dim: int | None) -> dict:
    embedder = SentenceTransformerEmbedder(model, dim=dim)
    embedder.embed(["прогрев"], "passage")  # загрузка модели не входит в замер скорости
    _, rules = load_default_checklist()
    rules = {r.code: r for r in rules}
    started = time.perf_counter()
    vectors = {name: embedder.embed([b.text for b in blocks], "passage") for name, blocks in docs.items()}
    n_blocks = sum(len(b) for b in docs.values())
    encode = time.perf_counter() - started
    hits = {k: 0 for k in KS}
    rr = 0.0
    for item in expected:
        blocks = docs[item["doc"]]
        query = embedder.embed([rule_query_text(rules[item["rule"]])], "query")[0]
        scores = [sum(a * b for a, b in zip(query, v, strict=True)) for v in vectors[item["doc"]]]
        ranking = sorted(range(len(blocks)), key=lambda i: -scores[i])
        relevant = {i for i, b in enumerate(blocks) if _contains(b.text, item["quote"])}
        rank = next((pos + 1 for pos, i in enumerate(ranking) if i in relevant), None)
        if rank:
            rr += 1 / rank
            for k in KS:
                hits[k] += rank <= k
    n = len(expected)
    return {
        "model": model,
        **{f"recall@{k}": hits[k] / n for k in KS},
        "mrr": rr / n,
        "blocks_per_s": n_blocks / encode if encode else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="+")
    parser.add_argument("--expected", type=Path, help="JSON со списком {rule, doc, quote}")
    parser.add_argument("--doc", action="append", default=[], help="имя=путь к документу для своего эталона")
    parser.add_argument("--dim", type=int, help="размерность, если модель не 768 (только для сравнения)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        if args.expected:
            expected = json.loads(args.expected.read_text())
            paths = dict(item.split("=", 1) for item in args.doc)
        else:
            from bench.samples import EXPECTED, make_contract_docx, make_notice_docx

            expected = EXPECTED
            paths = {"contract": make_contract_docx(Path(tmp) / "c.docx"), "notice": make_notice_docx(Path(tmp) / "n.docx")}
        docs = {name: blocks_from_extraction(extract_document(path)) for name, path in paths.items()}
        rows = [evaluate(model, docs, expected, args.dim) for model in args.models]
    header = f"{'модель':<55}" + "".join(f"{'R@' + str(k):>7}" for k in KS) + f"{'MRR':>7}{'блок/с':>9}"
    print(header)
    for row in rows:
        print(f"{row['model']:<55}" + "".join(f"{row[f'recall@{k}']:>7.2f}" for k in KS)
              + f"{row['mrr']:>7.2f}{row['blocks_per_s']:>9.1f}")
    print(f"\nзапросов: {len(expected)}; эталон синтетический, решение — по своим пакетам" if not args.expected else "")


if __name__ == "__main__":
    main()
