"""`extract_batch_meta`: предмет закупки для `{предмет_закупки}` и шапки пакета.

Предмет ищется в извещении (если его нет — во всех документах пакета): сначала по
шаблону строки «Наименование объекта закупки: …», затем коротким вызовом LLM по началу
документа. Не найден — None, анализ идёт дальше («не указан» в инструкциях правил).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel

from ml.analysis.blocks import BlockLike, estimate_tokens
from ml.analysis.llm import LLMClient, default_client
from ml.analysis.prompts import meta_prompt
from ml.config import get_settings
from ml.errors import LLMError

log = logging.getLogger(__name__)

_PATTERNS = [
    re.compile(r"(?:наименование\s+объекта\s+закупки|объект\s+закупки|предмет\s+(?:закупки|контракта|договора|аукциона))"
               r"\s*[:\-–—]\s*(?P<subject>.{5,300})", re.IGNORECASE),
]
_NOTICE = re.compile(r"извещ|notice", re.IGNORECASE)
META_TOKENS = 3000
MAX_SUBJECT = 300


class _Meta(BaseModel):
    subject: str | None


def _clean(subject: str | None) -> str | None:
    if not subject:
        return None
    subject = re.sub(r"\s+", " ", subject).strip().strip("«»\"'").strip()
    subject = re.sub(r"[.;,:\s]+$", "", subject)
    if len(subject) < 3 or subject.lower() in ("null", "не указан", "нет"):
        return None
    return subject[:MAX_SUBJECT]


def _is_notice(filename: str, blocks: Sequence[BlockLike]) -> bool:
    if _NOTICE.search(filename):
        return True
    head = " ".join(b.text for b in blocks[:3]).upper()
    return "ИЗВЕЩЕНИЕ" in head


def extract_batch_meta(
    documents: Sequence[tuple[str, Sequence[BlockLike]]],
    *,
    llm: LLMClient | None = None,
    use_llm: bool = True,
) -> str | None:
    """Предмет закупки по документам пакета: [(имя файла, блоки)] → строка или None."""
    if not documents:
        return None
    ordered = sorted(documents, key=lambda d: not _is_notice(d[0], d[1]))
    for filename, blocks in ordered:
        for block in sorted(blocks, key=lambda b: b.order)[:300]:
            for pattern in _PATTERNS:
                m = pattern.search(block.text)
                if m:
                    subject = _clean(m.group("subject"))
                    if subject:
                        log.info("batch_meta: предмет закупки по шаблону из %s: %s", filename, subject)
                        return subject
    if not use_llm or (llm is None and not get_settings().llm_model):
        return None
    filename, blocks = ordered[0]
    parts, tokens = [], 0
    for block in sorted(blocks, key=lambda b: b.order):
        text = f"{block.number} {block.text}" if getattr(block, "number", None) else block.text
        tokens += estimate_tokens(text)
        if tokens > META_TOKENS:
            break
        parts.append(text)
    if not parts:
        return None
    client = llm or default_client()
    meta_model = get_settings().llm_meta_model
    if meta_model and llm is None:
        client = LLMClient(model=meta_model)
    try:
        # Без рассуждений при любом LLM_REASONING: с ними ответ не укладывался в 200 токенов и приходил
        # пустым — 2 из 3 вызовов на тексте контракта у deepseek-v4.1-flash и v4-flash-0731 (замер 27.10)
        answer = client.chat(meta_prompt("\n".join(parts)), _Meta, schema_name="batch_meta", max_tokens=200,
                             reasoning="none")
    except LLMError as exc:
        log.warning("batch_meta: LLM недоступна, предмет не определён: %s", exc)
        return None
    subject = _clean(answer.data.subject)
    log.info("batch_meta: предмет закупки по LLM из %s: %s", filename, subject)
    return subject
