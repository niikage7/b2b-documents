"""Сквозной прогон ML-части на пакете: время этапов и полнота и точность по правилам.

Этапы как в конвейере: извлечение текста (с OCR) → блоки и фрагменты → эмбеддинги →
предмет закупки → группы правил параллельно. Для замера «20 страниц меньше 2 минут»
берите `--pages 20`. Без `LLM_MODEL` и `LLM_API_KEY` выполняются только извлечение и
эмбеддинги.

    python -m bench.pipeline                               # синтетический пакет
    python -m bench.pipeline --pages 20 --scan             # скан на 20 страниц
    python -m bench.pipeline a.docx b.pdf --expected expected.json

Эталон — JSON со списком {"rule", "doc", "quote"}, где `doc` — имя файла без расширения.
Находка совпала с эталоном, если правило и документ те же, а цитаты пересекаются.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapidfuzz import fuzz

from contracts.analysis import FindingCandidate, LLMUsage
from ml.analysis.anchor import normalize_map
from ml.analysis.batch_meta import extract_batch_meta
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks
from ml.analysis.embeddings import default_embedder
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.search import InMemorySearch
from ml.checklist import load_default_checklist
from ml.config import get_settings
from ml.extract import extract_document


def quotes_match(expected: str, found: str) -> bool:
    a, b = normalize_map(expected)[0], normalize_map(found)[0]
    return a in b or b in a or fuzz.partial_ratio(a, b) >= 85


def score(found: dict[str, list[FindingCandidate]], expected: list[dict]) -> dict[str, dict]:
    rules = sorted({e["rule"] for e in expected} | {c.rule_code for cands in found.values() for c in cands})
    out = {}
    for code in rules:
        exp = [e for e in expected if e["rule"] == code]
        got = [(doc, c) for doc, cands in found.items() for c in cands if c.rule_code == code]
        tp_exp = sum(any(doc == e["doc"] and quotes_match(e["quote"], c.quote) for doc, c in got) for e in exp)
        tp_got = sum(any(doc == e["doc"] and quotes_match(e["quote"], c.quote) for e in exp) for doc, c in got)
        out[code] = {
            "expected": len(exp),
            "found": len(got),
            "precision": tp_got / len(got) if got else None,
            "recall": tp_exp / len(exp) if exp else None,
        }
    return out


def run(paths: list[Path], expected: list[dict], *, subject: str | None = None) -> None:
    settings = get_settings()
    timings: dict[str, dict[str, float]] = defaultdict(dict)
    docs = {}
    for path in paths:
        started = time.perf_counter()
        result = extract_document(path)
        timings[path.stem]["извлечение"] = time.perf_counter() - started
        blocks = blocks_from_extraction(result)
        docs[path.stem] = (path, result, blocks)
        print(f"{path.name}: {result.kind}, страниц {result.pages}, блоков {len(blocks)}"
              + (f", OCR {result.ocr_confidence:.3f}" if result.ocr_confidence else ""))

    search = InMemorySearch()
    embedder = default_embedder()
    for name, (_, _, blocks) in docs.items():
        started = time.perf_counter()
        search.add(name, chunk_blocks(blocks), embedder=embedder)
        timings[name]["эмбеддинги"] = time.perf_counter() - started

    if not (settings.llm_model and settings.llm_api_key):
        print("\nLLM не настроена (LLM_MODEL, LLM_API_KEY): анализ пропущен")
        _print_timings(timings)
        return

    started = time.perf_counter()
    subject = subject or extract_batch_meta([(p.name, b) for p, _, b in docs.values()])
    meta_time = time.perf_counter() - started
    print(f"\nПредмет закупки: {subject or '—'} ({meta_time:.1f} с)")

    _, rules = load_default_checklist()
    groups = group_rules(rules)
    found: dict[str, list[FindingCandidate]] = {}
    usage = LLMUsage()
    dropped = 0
    for name, (_, _, blocks) in docs.items():
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(
                lambda g, blocks=blocks, name=name: analyze_rule_group(
                    blocks, g, subject=subject, document_id=name, search=search, embedder=embedder),
                groups,
            ))
        timings[name]["анализ"] = time.perf_counter() - started
        found[name] = [c for r in results for c in r.candidates]
        dropped += sum(len(r.dropped) for r in results)
        for r in results:
            usage.add(r.usage)
    _print_timings(timings)
    total = sum(len(v) for v in found.values())
    print(f"\nНаходок: {total}, отброшено ответов LLM: {dropped} ({dropped / max(1, total + dropped):.0%})")
    print(f"LLM: вызовов {usage.calls}, из кеша {usage.cached_calls}, токенов {usage.prompt_tokens} + "
          f"{usage.completion_tokens}, стоимость ${usage.cost:.4f}")
    if expected:
        print(f"\n{'правило':<8}{'ожидалось':>10}{'найдено':>9}{'точность':>10}{'полнота':>9}")
        for code, row in score(found, expected).items():
            p = "—" if row["precision"] is None else f"{row['precision']:.2f}"
            r = "—" if row["recall"] is None else f"{row['recall']:.2f}"
            print(f"{code:<8}{row['expected']:>10}{row['found']:>9}{p:>10}{r:>9}")
    for name, cands in found.items():
        for c in cands:
            print(f"  {name} {c.rule_code} [{c.confidence:.2f}] {c.quote[:90]}")


def _print_timings(timings: dict[str, dict[str, float]]) -> None:
    print(f"\n{'документ':<24}{'этап':<14}{'секунд':>8}")
    for name, stages in timings.items():
        for stage, seconds in stages.items():
            print(f"{name:<24}{stage:<14}{seconds:>8.1f}")
        print(f"{name:<24}{'итого':<14}{sum(stages.values()):>8.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--subject")
    parser.add_argument("--pages", type=int, help="синтетический договор такой длины")
    parser.add_argument("--scan", action="store_true", help="синтетический договор как скан")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if args.files:
            paths = args.files
            expected = json.loads(args.expected.read_text()) if args.expected else []
        else:
            from bench.samples import EXPECTED, long_contract, make_contract_pdf, make_notice_docx
            from bench.scangen import make_scan

            contract = make_contract_pdf(tmpdir / "contract.pdf", items=long_contract(args.pages or 2))
            if args.scan:
                contract = make_scan(contract, tmpdir / "contract-scan.pdf")
            paths = [contract, make_notice_docx(tmpdir / "notice.docx")]
            expected = [dict(e, doc=contract.stem if e["doc"] == "contract" else "notice") for e in EXPECTED]
        run(paths, expected, subject=args.subject)


if __name__ == "__main__":
    main()
