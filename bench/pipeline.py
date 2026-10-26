"""Сквозной прогон ML-части на пакете: время этапов и полнота и точность по правилам.

Этапы как в конвейере: извлечение текста (с OCR) → блоки и фрагменты → эмбеддинги →
предмет закупки → группы правил параллельно. Для замера «20 страниц меньше 2 минут»
берите `--pages 20`. Без `LLM_MODEL` и `LLM_API_KEY` выполняются только извлечение и
эмбеддинги.

    python -m bench.pipeline                               # синтетический пакет
    python -m bench.pipeline --pages 20 --scan             # скан на 20 страниц
    python -m bench.pipeline a.docx b.pdf --expected expected.json
    python -m bench.pipeline --pages 20 --reasoning default,low,none --repeat 3

`--reasoning` сравнивает режимы рассуждений модели (`LLM_REASONING`, только OpenRouter):
анализ повторяется для каждого режима без кеша LLM, режимы чередуются, чтобы задержка
провайдера в разное время не легла на один режим. Извлечение и эмбеддинги — один раз.

Эталон — JSON со списком {"rule", "doc", "quote"}, где `doc` — имя файла без расширения.
Находка совпала с эталоном, если правило и документ те же, а цитаты пересекаются.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from rapidfuzz import fuzz

from contracts.analysis import FindingCandidate, LLMUsage
from ml.analysis.anchor import normalize_map
from ml.analysis.batch_meta import extract_batch_meta
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks
from ml.analysis.embeddings import default_embedder
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.llm import REASONING_EFFORTS, LLMClient
from ml.analysis.search import InMemorySearch
from ml.checklist import load_default_checklist
from ml.config import get_settings
from ml.errors import MLError
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
            "tp_expected": tp_exp,
            "tp_found": tp_got,
            "precision": tp_got / len(got) if got else None,
            "recall": tp_exp / len(exp) if exp else None,
        }
    return out


@dataclass
class Analysis:
    found: dict[str, list[FindingCandidate]]
    usage: LLMUsage
    dropped: int
    seconds: dict[str, float]


def analyze(docs: dict, groups, *, subject, search, embedder, llm: LLMClient | None = None) -> Analysis:
    """Группы правил параллельно по каждому документу, как задачи `analyze_rule_group`."""
    found: dict[str, list[FindingCandidate]] = {}
    seconds: dict[str, float] = {}
    usage = LLMUsage()
    dropped = 0
    for name, (_, _, blocks) in docs.items():
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(
                lambda g, blocks=blocks, name=name: analyze_rule_group(
                    blocks, g, subject=subject, document_id=name, search=search, embedder=embedder, llm=llm),
                groups,
            ))
        seconds[name] = time.perf_counter() - started
        found[name] = [c for r in results for c in r.candidates]
        dropped += sum(len(r.dropped) for r in results)
        for r in results:
            usage.add(r.usage)
    return Analysis(found, usage, dropped, seconds)


def run(paths: list[Path], expected: list[dict], *, subject: str | None = None,
        reasoning: list[str] | None = None, repeat: int = 1) -> None:
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
    if reasoning:
        _print_timings(timings)
        compare_reasoning(docs, groups, expected, modes=reasoning, repeat=repeat,
                          subject=subject, search=search, embedder=embedder)
        return

    analysis = analyze(docs, groups, subject=subject, search=search, embedder=embedder)
    for name, seconds in analysis.seconds.items():
        timings[name]["анализ"] = seconds
    _print_timings(timings)
    found, usage, dropped = analysis.found, analysis.usage, analysis.dropped
    total = sum(len(v) for v in found.values())
    print(f"\nНаходок: {total}, отброшено ответов LLM: {dropped} ({dropped / max(1, total + dropped):.0%})")
    print(f"LLM: {_usage_line(usage)}")
    if expected:
        print(f"\n{'правило':<8}{'ожидалось':>10}{'найдено':>9}{'точность':>10}{'полнота':>9}")
        for code, row in score(found, expected).items():
            p = "—" if row["precision"] is None else f"{row['precision']:.2f}"
            r = "—" if row["recall"] is None else f"{row['recall']:.2f}"
            print(f"{code:<8}{row['expected']:>10}{row['found']:>9}{p:>10}{r:>9}")
    for name, cands in found.items():
        for c in cands:
            print(f"  {name} {c.rule_code} [{c.confidence:.2f}] {c.quote[:90]}")


def compare_reasoning(docs: dict, groups, expected: list[dict], *, modes: list[str], repeat: int,
                      subject, search, embedder) -> None:
    """Анализ в каждом режиме рассуждений `repeat` раз, без кеша LLM: время, токены, качество."""
    base = replace(get_settings(), llm_cache=False)
    rows = []
    for i in range(repeat):
        # Порядок режимов меняется от прогона к прогону: ни один режим не идёт всегда первым
        for mode in modes if i % 2 == 0 else modes[::-1]:
            client = LLMClient(replace(base, llm_reasoning=mode))
            if mode != "default" and client.reasoning is None:
                print("\nLLM_REASONING действует только для OpenRouter: сравнение режимов не имеет смысла")
                return
            print(f"\nрежим {mode}, прогон {i + 1} из {repeat}…", flush=True)
            started = time.perf_counter()
            try:
                analysis = analyze(docs, groups, subject=subject, search=search, embedder=embedder, llm=client)
            except MLError as exc:
                # Сбой режима — тоже результат: записываем и идём дальше, а не теряем остальные режимы
                print(f"  ошибка: {exc}")
                rows.append((mode, i + 1, None, time.perf_counter() - started, exc.message))
                continue
            if mode != "default" and client.reasoning_active is None:
                print(f"  модель не приняла reasoning={mode}: прогон шёл с рассуждениями по умолчанию")
            rows.append((mode, i + 1, analysis, sum(analysis.seconds.values()), None))

    print(f"\n{'режим':<9}{'прогон':>7}{'анализ, с':>11}{'вызовов':>9}{'вход':>8}{'выход':>8}"
          f"{'рассужд.':>10}{'$':>9}{'находок':>9}{'отброш.':>9}{'полнота':>10}{'точность':>10}")
    for mode, n, a, seconds, error in rows:
        if a is None:
            print(f"{mode:<9}{n:>7}{seconds:>11.1f}  ошибка: {error}")
            continue
        u = a.usage
        found = sum(len(v) for v in a.found.values())
        recall, precision = _totals(a.found, expected)
        print(f"{mode:<9}{n:>7}{seconds:>11.1f}{u.calls:>9}{u.prompt_tokens:>8}"
              f"{u.completion_tokens:>8}{u.reasoning_tokens:>10}{u.cost:>9.4f}{found:>9}{a.dropped:>9}"
              f"{recall:>10}{precision:>10}")

    print("\nПо режимам (время анализа — медиана и разброс по прогонам; токены и находки — в среднем на прогон):")
    for mode in modes:
        failed = sum(1 for m, _, a, _, _ in rows if m == mode and a is None)
        runs = [a for m, _, a, _, _ in rows if m == mode and a is not None]
        if not runs:
            print(f"  {mode}: все прогоны ({failed}) с ошибкой")
            continue
        times = [sum(a.seconds.values()) for a in runs]
        output = statistics.mean(a.usage.completion_tokens for a in runs)
        thinking = statistics.mean(a.usage.reasoning_tokens for a in runs)
        cost = statistics.mean(a.usage.cost for a in runs)
        found = statistics.mean(sum(len(v) for v in a.found.values()) for a in runs)
        print(f"  {mode}: {statistics.median(times):.1f} с ({min(times):.1f}–{max(times):.1f}), "
              f"выход {output:.0f} ток., из них рассуждения {thinking:.0f}, ${cost:.4f}, находок {found:.1f}"
              + (f"; с ошибкой — {failed} из {failed + len(runs)} прогонов" if failed else ""))
    if expected:
        print("  Полнота — найдено ожидаемых находок; точность — сколько находок совпало с эталоном.")


def _totals(found: dict[str, list[FindingCandidate]], expected: list[dict]) -> tuple[str, str]:
    if not expected:
        return "—", "—"
    rows = score(found, expected).values()
    recall = f"{sum(r['tp_expected'] for r in rows)} из {sum(r['expected'] for r in rows)}"
    precision = f"{sum(r['tp_found'] for r in rows)} из {sum(r['found'] for r in rows)}"
    return recall, precision


def _usage_line(usage: LLMUsage) -> str:
    return (f"вызовов {usage.calls}, из кеша {usage.cached_calls}, токенов {usage.prompt_tokens} + "
            f"{usage.completion_tokens} (рассуждения {usage.reasoning_tokens}), стоимость ${usage.cost:.4f}")


def _print_timings(timings: dict[str, dict[str, float]]) -> None:
    print(f"\n{'документ':<24}{'этап':<14}{'секунд':>8}")
    for name, stages in timings.items():
        for stage, seconds in stages.items():
            print(f"{name:<24}{stage:<14}{seconds:>8.1f}")
        print(f"{name:<24}{'итого':<14}{sum(stages.values()):>8.1f}")


def _modes(value: str) -> list[str]:
    modes = [m.strip().lower() for m in value.split(",") if m.strip()]
    unknown = [m for m in modes if m not in ("default", *REASONING_EFFORTS)]
    if not modes or unknown:
        raise argparse.ArgumentTypeError(
            f"режимы через запятую из: default, {', '.join(REASONING_EFFORTS)}")
    return modes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--subject")
    parser.add_argument("--pages", type=int, help="синтетический договор такой длины")
    parser.add_argument("--scan", action="store_true", help="синтетический договор как скан")
    parser.add_argument("--reasoning", type=_modes,
                        help="сравнить режимы рассуждений, например default,none (только OpenRouter)")
    parser.add_argument("--repeat", type=int, default=1, help="прогонов анализа на режим при --reasoning")
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
        run(paths, expected, subject=args.subject, reasoning=args.reasoning, repeat=max(1, args.repeat))


if __name__ == "__main__":
    main()
