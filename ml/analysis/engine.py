"""Движок правил (FR-4.1, FR-9.2): группа правил → вызовы LLM → кандидаты в находки.

Движок ничего не знает о конкретных правилах: правило — данные (инструкция, переменные,
уровень, тип). Два типа:

- `semantic_llm` — для каждого правила фрагменты документа в пределах бюджета токенов,
  отобранные гибридным поиском (`ml.analysis.search`) по строке «название + описание»;
  фрагменты всех правил группы объединяются, один вызов LLM на группу;
- `llm` — весь документ окнами около 6000 токенов, один вызов на окно и группу.

Цитата из ответа привязывается к тексту блока; раздел и страницу находке даёт блок, а не
ответ LLM. Ответ с вердиктом `risk=false` (условие проверено, риска нет) находкой не
становится. Уровень находки — уровень правила.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from contracts.analysis import (
    DroppedFinding,
    FindingCandidate,
    LLMGroupResponse,
    LLMUsage,
    RuleGroupResult,
    RuleSpec,
)
from ml.analysis.anchor import anchor_quote
from ml.analysis.blocks import BlockLike, Fragment, Selection, estimate_tokens, excerpt, select_hit
from ml.analysis.embeddings import Embedder, embed_texts
from ml.analysis.llm import LLMClient, default_client
from ml.analysis.prompts import group_prompt
from ml.analysis.search import ChunkSearch, LexicalIndex, rank_chunks, take_budget
from ml.analysis.templates import rule_query_text, rule_search_text
from ml.config import get_settings

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]
_CODE = re.compile(r"[\s\-_.]")


def group_rules(rules: Sequence[RuleSpec], size: int | None = None) -> list[list[RuleSpec]]:
    """Включённые правила → группы по 3–4 для параллельных задач `analyze_rule_group`.

    Типы не смешиваются: `llm` читает весь документ окнами, `semantic_llm` — найденные фрагменты.
    """
    size = size or get_settings().rules_per_group
    groups: list[list[RuleSpec]] = []
    for kind in ("semantic_llm", "llm"):
        part = sorted((r for r in rules if r.enabled and r.kind == kind), key=lambda r: r.code)
        groups.extend(part[i : i + size] for i in range(0, len(part), size))
    return groups


@dataclass
class _Call:
    rules: list[RuleSpec]
    fragments: list[Fragment]
    rule_fragments: dict[str, list[int]]


def _fragments_from_blocks(ordered: list[BlockLike], selected: set[str]) -> list[Fragment]:
    """Выбранные блоки → фрагменты: подряд идущие блоки документа объединяются в один."""
    fragments: list[Fragment] = []
    prev_pos = None
    for pos, block in enumerate(ordered):
        if block.id not in selected:
            continue
        if prev_pos is None or pos != prev_pos + 1:
            fragments.append(Fragment(index=len(fragments) + 1))
        fragments[-1].blocks.append(block)
        prev_pos = pos
    return fragments


def _semantic_call(
    rules: list[RuleSpec],
    ordered: list[BlockLike],
    *,
    document_id: str,
    search: ChunkSearch,
    embedder: Embedder | None,
    budget_tokens: int,
) -> _Call | None:
    known = {b.id for b in ordered}
    chunks = search.chunks(document_id)
    if not chunks:
        return None
    lexical = LexicalIndex([c.text for c in chunks])
    missing = [r for r in rules if not r.query_embedding]
    vectors = dict(zip(
        [r.code for r in missing],
        embed_texts([rule_query_text(r) for r in missing], "query", embedder=embedder) if missing else [],
        strict=True,
    ))
    per_rule: dict[str, set[str]] = {}
    windows: Selection = {}
    for rule in rules:
        vector = rule.query_embedding or vectors[rule.code]
        hits = take_budget(rank_chunks(chunks, vector, rule_search_text(rule), lexical=lexical), budget_tokens)
        per_rule[rule.code] = {bid for hit in hits for bid in hit.block_ids if bid in known}
        for hit in hits:
            select_hit(windows, hit)
        log.debug("analysis: %s: фрагментов %d из %d", rule.code, len(hits), len(chunks))
    selected = set().union(*per_rule.values()) if per_rule else set()
    if not selected:
        return None
    fragments = _fragments_from_blocks(ordered, selected)
    for fragment in fragments:
        # От длинного блока в запрос идут только найденные окна; цитата привязывается к блоку целиком
        fragment.excerpts = {b.id: excerpt(b.text, windows[b.id]) for b in fragment.blocks if windows.get(b.id)}
    by_block = {b.id: f.index for f in fragments for b in f.blocks}
    rule_fragments = {code: sorted({by_block[bid] for bid in ids}) for code, ids in per_rule.items()}
    return _Call(rules, fragments, rule_fragments)


def _window_calls(rules: list[RuleSpec], ordered: list[BlockLike], window_tokens: int) -> list[_Call]:
    calls: list[_Call] = []
    current: list[BlockLike] = []
    tokens = 0
    for block in ordered:
        t = estimate_tokens(block.text) + 3
        if current and tokens + t > window_tokens:
            calls.append(_Call(rules, [Fragment(1, current)], {r.code: [1] for r in rules}))
            current, tokens = [], 0
        current.append(block)
        tokens += t
    if current:
        calls.append(_Call(rules, [Fragment(1, current)], {r.code: [1] for r in rules}))
    return calls


def _norm_code(code: str) -> str:
    return _CODE.sub("", code).upper()


def analyze_rule_group(
    blocks: Sequence[BlockLike],
    rules: Sequence[RuleSpec],
    *,
    subject: str | None = None,
    document_id: str | None = None,
    search: ChunkSearch | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    progress: ProgressFn | None = None,
    budget_tokens: int | None = None,
    window_tokens: int | None = None,
) -> RuleGroupResult:
    """Проверяет документ группой правил и возвращает кандидатов в находки.

    `blocks` — все блоки документа (поля из «Требований к бэкенду»), `search` и
    `document_id` нужны для `semantic_llm`; без них такие правила читают документ окнами.
    `budget_tokens` — сколько текста найденных фрагментов получает каждое правило.
    Ничего не пишет в БД: результат сохраняет `merge_findings`.
    """
    settings = get_settings()
    llm = llm or default_client()
    budget_tokens = budget_tokens or settings.search_budget_tokens
    window_tokens = window_tokens or settings.llm_window_tokens
    ordered = sorted(blocks, key=lambda b: b.order)
    active = [r for r in rules if r.enabled]
    result = RuleGroupResult(candidates=[])
    if not active or not ordered:
        result.checked_rules = [r.code for r in active]
        return result

    semantic = [r for r in active if r.kind == "semantic_llm"]
    whole = [r for r in active if r.kind == "llm"]
    calls: list[_Call] = []
    call = None
    if semantic and search is not None and document_id is not None:
        call = _semantic_call(semantic, ordered, document_id=document_id, search=search,
                              embedder=embedder, budget_tokens=budget_tokens)
    if call is not None:
        calls.append(call)
    elif semantic:
        # Нет поиска или индекс документа пуст: правила читают документ окнами, а не молчат
        log.info("analysis: поиск фрагментов недоступен, правила %s читают документ окнами",
                 ", ".join(r.code for r in semantic))
        whole = semantic + whole
    if whole:
        calls.extend(_window_calls(whole, ordered, window_tokens))

    total = len(calls)
    done = 0
    if progress:
        progress(0, total)

    def run(call: _Call):
        messages = group_prompt(call.rules, call.fragments, call.rule_fragments, subject=subject)
        return llm.chat(messages, LLMGroupResponse, schema_name="risk_findings")

    with ThreadPoolExecutor(max_workers=max(1, min(total, settings.llm_max_concurrency))) as pool:
        futures = [pool.submit(run, call) for call in calls]
        answers = []
        for call, future in zip(calls, futures, strict=True):
            answers.append((call, future.result()))
            done += 1
            if progress:
                progress(done, total)

    usage = LLMUsage()
    for call, answer in answers:
        usage.add(answer.usage)
        _collect(call, answer.data, result)
    result.usage = usage
    result.candidates = _dedupe(result.candidates, result.dropped)
    result.checked_rules = [r.code for r in active]
    if result.dropped:
        for d in result.dropped:
            log.info("analysis: находка отброшена: %s %s (балл %s): %.80s", d.rule_code, d.reason, d.best_score, d.quote)
    return result


def _collect(call: _Call, data: LLMGroupResponse, result: RuleGroupResult) -> None:
    rules = {_norm_code(r.code): r for r in call.rules}
    all_blocks = [b for f in call.fragments for b in f.blocks]
    by_index = {f.index: f for f in call.fragments}
    for answer in data.rules:
        rule = rules.get(_norm_code(answer.rule_code))
        if rule is None:
            for finding in answer.findings:
                result.dropped.append(DroppedFinding(rule_code=answer.rule_code, quote=finding.quote, reason="unknown_rule"))
            continue
        own = call.rule_fragments.get(rule.code) or list(by_index)
        own_blocks = [b for i in own if i in by_index for b in by_index[i].blocks]
        for finding in answer.findings:
            if not finding.quote.strip():
                result.dropped.append(DroppedFinding(rule_code=rule.code, quote="", reason="empty_quote"))
                continue
            if not finding.risk:
                # Модель проверила условие и риска не нашла: в отчёт не идёт, остаётся в логе
                result.dropped.append(DroppedFinding(rule_code=rule.code, quote=finding.quote, reason="no_risk"))
                continue
            preferred = by_index[finding.fragment].blocks if finding.fragment in by_index else own_blocks
            rest = [b for b in all_blocks if b not in preferred]
            anchor, score = anchor_quote(finding.quote, preferred, fallback=rest)
            if anchor is None and preferred is not own_blocks:
                anchor, score = anchor_quote(finding.quote, own_blocks, fallback=rest)
            if anchor is None:
                result.dropped.append(DroppedFinding(rule_code=rule.code, quote=finding.quote,
                                                     reason="quote_not_found", best_score=round(score, 1)))
                continue
            result.candidates.append(
                FindingCandidate(
                    rule_code=rule.code,
                    rule_id=rule.id,
                    rule_version_id=rule.version_id,
                    level=rule.level,
                    block_id=anchor.block.id,
                    start=anchor.start,
                    end=anchor.end,
                    quote=anchor.block.text[anchor.start : anchor.end],
                    explanation=finding.explanation.strip(),
                    recommendation=finding.recommendation.strip() or None,
                    confidence=round(finding.confidence, 3),
                    match_score=anchor.score,
                )
            )


def _dedupe(candidates: list[FindingCandidate], dropped: list[DroppedFinding]) -> list[FindingCandidate]:
    """Одно место — одна находка правила: пересекающиеся отрезки в блоке сливаются, остаётся увереннее."""
    kept: list[FindingCandidate] = []
    for cand in sorted(candidates, key=lambda c: (-c.confidence, -(c.end - c.start))):
        clash = next(
            (k for k in kept if k.rule_code == cand.rule_code and k.block_id == cand.block_id
             and k.start < cand.end and cand.start < k.end),
            None,
        )
        if clash is not None:
            dropped.append(DroppedFinding(rule_code=cand.rule_code, quote=cand.quote, reason="duplicate"))
            continue
        kept.append(cand)
    return kept
