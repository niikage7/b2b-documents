"""Стенд выбора bi-encoder: находит ли строка поиска правила текст с риском.

Запрос — строка поиска правила («название + описание», `rule_query_text`), как у
`semantic_llm` в анализе. Два набора:

- `clauses` (по умолчанию) — пункты из `bench.clauses`, перемешанные со служебными в документ
  нужной длины и нарезанные `chunk_blocks`, как в `index_chunks`. Для каждого правила
  `semantic_llm` и режима поиска (векторный, BM25, гибрид) считается доля пунктов с риском
  в фрагментах, которые правило получает при бюджете токенов, и сколько токенов нужно, чтобы
  найти все пункты с риском: по нему видно, хватает ли `SEARCH_BUDGET_TOKENS`. Пункты с
  риском рассыпаны по документу — это труднее, чем в настоящем договоре, где условия одной
  темы стоят в одном разделе;
- `package` — синтетический пакет `bench.samples` и ожидаемые цитаты `EXPECTED`: recall@k
  блока с цитатой и MRR векторным поиском по блокам, и доля цитат во фрагментах, которые
  правило получает при бюджете токенов, — гибридом по фрагментам, как в анализе.

    python -m bench.embeddings intfloat/multilingual-e5-base sergeyzh/BERTA deepvk/USER2-base
    python -m bench.embeddings intfloat/multilingual-e5-base --chunk-tokens 60,100,400
    python -m bench.embeddings deepvk/USER-bge-m3 --pages 40
    python -m bench.embeddings intfloat/multilingual-e5-base --set package
    python -m bench.embeddings sergeyzh/BERTA --seeds 1 --llm    # и сквозная проверка с LLM, около $0,005

`--llm` прогоняет анализ правилами `semantic_llm` с LLM из `LLM_MODEL` на первом перемешивании
`clauses`: полнота — пункты с риском, у которых есть находка своего правила, точность — находки
на пунктах с риском этого правила.

Свой эталон для `package`: `--expected file.json` со списком {"rule", "doc", "quote"} и
`--doc имя=путь`. Модели скачиваются с Hugging Face при первом запуске. Размерность
не проверяется: стенд сравнивает и модели не на 768, схему тогда нужно менять.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

from ml.analysis.anchor import normalize_map
from ml.analysis.blocks import SimpleBlock, blocks_from_extraction, chunk_blocks, estimate_tokens
from ml.analysis.embeddings import SentenceTransformerEmbedder
from ml.analysis.search import LexicalIndex, rank_chunks, take_budget
from ml.analysis.templates import rule_query_text, rule_search_text
from ml.checklist import load_default_checklist
from ml.config import get_settings

KS = (1, 3, 5, 8)
BUDGETS = (800, 1600, 3200)
MODES = ("dense", "lexical", "hybrid")
CLAUSES_PER_PAGE = 20


def _contains(block_text: str, quote: str) -> bool:
    return normalize_map(quote)[0] in normalize_map(block_text)[0]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def evaluate_package(embedder: SentenceTransformerEmbedder, docs: dict[str, list], expected: list[dict]) -> dict:
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
        scores = [_dot(query, v) for v in vectors[item["doc"]]]
        ranking = sorted(range(len(blocks)), key=lambda i: -scores[i])
        relevant = {i for i, b in enumerate(blocks) if _contains(b.text, item["quote"])}
        rank = next((pos + 1 for pos, i in enumerate(ranking) if i in relevant), None)
        if rank:
            rr += 1 / rank
            for k in KS:
                hits[k] += rank <= k
    n = len(expected)
    return {
        **{f"recall@{k}": hits[k] / n for k in KS},
        "mrr": rr / n,
        "per_s": n_blocks / encode if encode else 0.0,
        **budget_recall(embedder, docs, expected, rules),
    }


def budget_recall(embedder: SentenceTransformerEmbedder, docs: dict[str, list], expected: list[dict],
                  rules: dict) -> dict:
    """Доля цитат во фрагментах правила при бюджете: фрагменты, гибрид и бюджет — как в анализе."""
    chunks, lexical = {}, {}
    for name, blocks in docs.items():
        chunks[name] = chunk_blocks(blocks)
        for chunk, vector in zip(chunks[name], embedder.embed([c.text for c in chunks[name]], "passage"), strict=True):
            chunk.embedding = vector
        lexical[name] = LexicalIndex([c.text for c in chunks[name]])
    covered = {b: 0 for b in BUDGETS}
    for item in expected:
        rule = rules[item["rule"]]
        vector = embedder.embed([rule_query_text(rule)], "query")[0]
        hits = rank_chunks(chunks[item["doc"]], vector, rule_search_text(rule), mode="hybrid",
                           lexical=lexical[item["doc"]])
        quote = normalize_map(item["quote"])[0]
        for budget in BUDGETS:
            covered[budget] += any(quote in normalize_map(h.text)[0] for h in take_budget(hits, budget))
    return {f"budget@{b}": covered[b] / len(expected) for b in BUDGETS}


def clauses_document(pages: int, seed: int) -> tuple[list[SimpleBlock], dict[str, frozenset[str]]]:
    """Пункты `bench.clauses` и служебные пункты в случайном порядке: около 20 пунктов на страницу."""
    from bench.clauses import CLAUSES, package_clauses
    from bench.samples import FILLER

    items = [(c.text, c.rules) for c in CLAUSES]
    items += [(c.text, c.rules) for c in package_clauses()]
    filler = itertools.cycle([i.text for i in FILLER if i.level > 0])
    while len(items) < pages * CLAUSES_PER_PAGE:
        items.append((next(filler), frozenset()))
    random.Random(seed).shuffle(items)
    blocks = [SimpleBlock(id=f"b{i}", order=i, text=text, number=f"{i + 1}.", page=i // CLAUSES_PER_PAGE + 1)
              for i, (text, _) in enumerate(items)]
    labels = {b.id: rules for b, (_, rules) in zip(blocks, items, strict=True)}
    return blocks, labels


def evaluate_clauses(embedder: SentenceTransformerEmbedder, blocks: list[SimpleBlock],
                     labels: dict[str, frozenset[str]], *, chunk_tokens: int, overlap: int,
                     modes: tuple[str, ...]) -> dict:
    """Доля пунктов с риском в фрагментах, которые правило получает при каждом бюджете токенов."""
    _, rules = load_default_checklist()
    rules = [r for r in rules if r.kind == "semantic_llm"]
    chunks = chunk_blocks(blocks, max_tokens=chunk_tokens, overlap=overlap)
    started = time.perf_counter()
    for chunk, vector in zip(chunks, embedder.embed([c.text for c in chunks], "passage"), strict=True):
        chunk.embedding = vector
    encode = time.perf_counter() - started
    lexical = LexicalIndex([c.text for c in chunks])
    out: dict = {"chunks": len(chunks), "per_s": len(chunks) / encode if encode else 0.0}
    for mode in modes:
        covered = {b: 0 for b in BUDGETS}
        total = 0
        need: dict[str, int] = {}
        for rule in rules:
            risky = {bid for bid, codes in labels.items() if rule.code in codes}
            if not risky:
                continue
            vector = embedder.embed([rule_query_text(rule)], "query")[0]
            hits = rank_chunks(chunks, vector, rule_search_text(rule), mode=mode, lexical=lexical)
            for budget in BUDGETS:
                got = {bid for h in take_budget(hits, budget) for bid in h.block_ids}
                covered[budget] += len(risky & got)
            seen: set[str] = set()
            used = 0
            for hit in hits:
                seen |= risky & set(hit.block_ids)
                used += estimate_tokens(hit.text)
                if seen == risky:
                    break
            need[rule.code] = used
            total += len(risky)
        out[mode] = {
            **{f"recall@{b}": covered[b] / total for b in BUDGETS},
            "need_median": statistics.median(need.values()),
            "need_max": max(need.values()),
        }
    return out


def llm_check(blocks: list[SimpleBlock], labels: dict[str, frozenset[str]], embedder) -> None:
    """Сквозная проверка: поиск, LLM и привязка цитат на документе из пунктов `bench.clauses`."""
    from bench.samples import SUBJECT
    from contracts.analysis import LLMUsage
    from ml.analysis.engine import analyze_rule_group, group_rules
    from ml.analysis.search import InMemorySearch

    settings = get_settings()
    if not (settings.llm_model and settings.llm_api_key):
        print("\nLLM не настроена (LLM_MODEL, LLM_API_KEY): сквозная проверка пропущена")
        return
    _, rules = load_default_checklist()
    semantic = [r for r in rules if r.kind == "semantic_llm"]
    codes = {r.code for r in semantic}
    store = InMemorySearch()
    store.add("doc", chunk_blocks(blocks), embedder=embedder)
    risky = {(code, bid) for bid, rule_codes in labels.items() for code in rule_codes if code in codes}
    found: set[tuple[str, str]] = set()
    usage = LLMUsage()
    started = time.perf_counter()
    for group in group_rules(semantic):
        result = analyze_rule_group(blocks, group, subject=SUBJECT, document_id="doc", search=store,
                                    embedder=embedder)
        usage.add(result.usage)
        found |= {(c.rule_code, c.block_id) for c in result.candidates}
    seconds = time.perf_counter() - started
    hit = found & risky
    print(f"\nС LLM: полнота {len(hit)} из {len(risky)}, точность {len(hit)} из {len(found)}; вход "
          f"{usage.prompt_tokens}, выход {usage.completion_tokens} токенов, {seconds:.1f} с, ${usage.cost:.4f}")
    by_id = {b.id: b for b in blocks}
    for code, bid in sorted(risky - found):
        print(f"  пропуск {code}: {by_id[bid].text[:100]}")
    for code, bid in sorted(found - risky):
        print(f"  лишняя {code}: {by_id[bid].text[:100]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="+")
    parser.add_argument("--set", choices=("clauses", "package"), default="clauses")
    parser.add_argument("--pages", type=int, default=20, help="длина документа для набора clauses")
    parser.add_argument("--seeds", type=int, default=3, help="сколько перемешиваний усреднять для clauses")
    parser.add_argument("--chunk-tokens", default=str(get_settings().chunk_tokens),
                        help="размеры фрагмента через запятую, например 60,100,400")
    parser.add_argument("--overlap", type=int, default=get_settings().chunk_overlap, help="перекрытие фрагментов, блоков")
    parser.add_argument("--expected", type=Path, help="JSON со списком {rule, doc, quote} для набора package")
    parser.add_argument("--doc", action="append", default=[], help="имя=путь к документу для своего эталона")
    parser.add_argument("--llm", action="store_true", help="для clauses: сквозная проверка с LLM на первом перемешивании")
    args = parser.parse_args()

    rows = []
    if args.set == "clauses":
        documents = [clauses_document(args.pages, seed) for seed in range(args.seeds)]
        sizes = [int(x) for x in args.chunk_tokens.split(",")]
        for model in args.models:
            embedder = SentenceTransformerEmbedder(model, check_dim=False)
            started = time.perf_counter()
            embedder.embed(["прогрев"], "passage")  # загрузка модели не входит в замер скорости
            load = time.perf_counter() - started
            for size in sizes:
                runs = [evaluate_clauses(embedder, blocks, labels, chunk_tokens=size, overlap=args.overlap,
                                         modes=MODES) for blocks, labels in documents]
                rows.append((model, size, runs, load))
        print(f"Документ: {args.pages} стр.; перемешиваний: {args.seeds}; правила semantic_llm; "
              f"перекрытие фрагментов {args.overlap} блок.\n"
              "Доля пунктов с риском в фрагментах, отобранных при бюджете токенов на правило; "
              "«всех» — сколько токенов нужно, чтобы найти все пункты с риском правила\n")
        print(f"{'модель':<38}{'фрагм.':>7}{'режим':>9}" + "".join(f"{'B' + str(b):>7}" for b in BUDGETS)
              + f"{'всех, мед.':>12}{'всех, макс.':>13}{'фрагм./с':>10}{'загр., с':>10}")
        for model, size, runs, load in rows:
            for mode in MODES:
                avg = {key: statistics.mean(r[mode][key] for r in runs) for key in runs[0][mode]}
                print(f"{model:<38}{size:>7}{mode:>9}" + "".join(f"{avg[f'recall@{b}']:>7.2f}" for b in BUDGETS)
                      + f"{avg['need_median']:>12.0f}{max(r[mode]['need_max'] for r in runs):>13}"
                      + f"{statistics.mean(r['per_s'] for r in runs):>10.1f}{load:>10.1f}")
        print("\nПункты синтетические, рассыпаны по документу случайно; решение проверить на своих пакетах")
        if args.llm:
            llm_check(*documents[0], SentenceTransformerEmbedder(args.models[0], check_dim=False))
        return

    with tempfile.TemporaryDirectory() as tmp:
        from ml.extract import extract_document

        if args.expected:
            expected = json.loads(args.expected.read_text())
            paths = dict(item.split("=", 1) for item in args.doc)
        else:
            from bench.samples import EXPECTED, make_contract_docx, make_notice_docx

            expected = EXPECTED
            paths = {"contract": make_contract_docx(Path(tmp) / "c.docx"), "notice": make_notice_docx(Path(tmp) / "n.docx")}
        docs = {name: blocks_from_extraction(extract_document(path)) for name, path in paths.items()}
        for model in args.models:
            embedder = SentenceTransformerEmbedder(model, check_dim=False)
            embedder.embed(["прогрев"], "passage")
            rows.append((model, evaluate_package(embedder, docs, expected), 0.0))
    header = (f"{'модель':<55}" + "".join(f"{'R@' + str(k):>7}" for k in KS) + f"{'MRR':>7}{'блок/с':>9}"
              + "".join(f"{'B' + str(b):>7}" for b in BUDGETS))
    print(header)
    for model, row, _ in rows:
        print(f"{model:<55}" + "".join(f"{row[f'recall@{k}']:>7.2f}" for k in KS)
              + f"{row['mrr']:>7.2f}{row['per_s']:>9.1f}" + "".join(f"{row[f'budget@{b}']:>7.2f}" for b in BUDGETS))
    print("\nR@k и MRR — векторный поиск по блокам; B<бюджет> — доля цитат во фрагментах правила, гибрид")
    if not args.expected:
        print(f"\nзапросов: {len(expected)}; эталон синтетический, решение — по своим пакетам")


if __name__ == "__main__":
    main()
