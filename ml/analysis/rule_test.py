"""`test_rule` (FR-9.3): прогон правила до включения, без записи в БД, не дольше 60 с.

Тот же движок, что и в анализе пакета. На тексте из поля «Проверить на тексте» блоки и
фрагменты строятся в памяти; на загруженном документе Влад передаёт его блоки и поиск
по pgvector.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from contracts.analysis import RuleSpec, RuleTestFinding, RuleTestResult
from ml.analysis.blocks import BlockLike, blocks_from_text, chunk_blocks, section_of
from ml.analysis.embeddings import Embedder
from ml.analysis.engine import analyze_rule_group
from ml.analysis.llm import LLMClient
from ml.analysis.search import ChunkSearch, InMemorySearch
from ml.analysis.templates import validate_rule
from ml.config import get_settings
from ml.errors import RuleTemplateError, RuleTestTimeoutError, TextTooLongError

MAX_TEXT = 20_000
_TEXT_DOC = "__text__"


def test_rule(
    rule: RuleSpec,
    *,
    text: str | None = None,
    blocks: Sequence[BlockLike] | None = None,
    document_id: str | None = None,
    search: ChunkSearch | None = None,
    subject: str | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    timeout: float | None = None,
) -> RuleTestResult:
    """Находки правила на тексте или на блоках документа. Правило может быть несохранённым."""
    started = time.monotonic()
    errors, _ = validate_rule(rule)
    if errors:
        raise RuleTemplateError(errors[0].message)
    rule = rule.model_copy(update={"enabled": True})
    if text is not None:
        if len(text) > MAX_TEXT:
            raise TextTooLongError()
        blocks = blocks_from_text(text)
        store = InMemorySearch()
        if rule.kind == "semantic_llm":
            store.add(_TEXT_DOC, chunk_blocks(blocks), embedder=embedder)
        search, document_id = store, _TEXT_DOC
    if not blocks:
        return RuleTestResult(findings=[], duration_ms=int((time.monotonic() - started) * 1000))

    timeout = timeout or get_settings().test_rule_timeout
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        analyze_rule_group, blocks, [rule], subject=subject, document_id=document_id, search=search,
        llm=llm, embedder=embedder,
    )
    try:
        result = future.result(timeout=max(1.0, timeout - (time.monotonic() - started)))
    except FutureTimeout as exc:
        raise RuleTestTimeoutError() from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    by_id = {b.id: b for b in blocks}
    findings = []
    for cand in sorted(result.candidates, key=lambda c: (by_id[c.block_id].order, c.start)):
        block = by_id[cand.block_id]
        findings.append(
            RuleTestFinding(
                quote=cand.quote,
                block_id=cand.block_id,
                start=cand.start,
                end=cand.end,
                section_number=section_of(block),
                page=getattr(block, "page", None),
                level=cand.level,
                explanation=cand.explanation,
                recommendation=cand.recommendation,
                confidence=cand.confidence,
            )
        )
    dropped = sum(1 for d in result.dropped if d.reason == "quote_not_found")
    return RuleTestResult(findings=findings, duration_ms=int((time.monotonic() - started) * 1000), dropped=dropped)


test_rule.__test__ = False  # не тест pytest, несмотря на имя
