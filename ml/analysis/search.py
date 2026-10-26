"""Поиск фрагментов документа для правила: гибрид векторной близости и BM25.

Фрагменты одного документа читаются целиком (у документа их сотни, у 500 страниц — пара
тысяч), ранжируются здесь же в Python и отбираются по бюджету токенов, а не по числу:

- векторная близость (bi-encoder) ловит смысл, но размывается на длинных фрагментах;
- BM25 по основам слов ловит термины закона — «пеня», «обеспечение исполнения»,
  «гарантийный срок», — которые смысловая модель путает с соседними темами;
- ранги объединяются по Reciprocal Rank Fusion: шкалы двух оценок несопоставимы.

На стенде `bench.embeddings` (20 страниц, пункты с риском рассыпаны по документу) top-8
фрагментов по 400 токенов находили половину пунктов с риском; фрагменты по 100 токенов
с тем же бюджетом и гибридом — 0,98 (замер 27.10, docs/ml-plan.md).

`PgVectorSearch` читает таблицу `chunk` из «Требований к бэкенду» (`id`, `document_id`,
`block_ids`, `text`, `embedding`).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Literal, Protocol

from ml.analysis.blocks import Chunk, ChunkHit, estimate_tokens
from ml.analysis.embeddings import Embedder, embed_texts
from ml.config import get_settings

SearchMode = Literal["hybrid", "dense", "lexical"]
RRF_K = 60


class ChunkSearch(Protocol):
    def chunks(self, document_id: str) -> list[Chunk]:
        """Все фрагменты документа с эмбеддингами, в порядке документа."""
        ...


class InMemorySearch:
    """Фрагменты в памяти: `test_rule` по тексту, тесты, стенды."""

    def __init__(self, chunks_by_document: dict[str, list[Chunk]] | None = None):
        self.store: dict[str, list[Chunk]] = chunks_by_document or {}

    def add(self, document_id: str, chunks: list[Chunk], *, embedder: Embedder | None = None) -> None:
        missing = [c for c in chunks if c.embedding is None]
        if missing:
            vectors = embed_texts([c.text for c in missing], "passage", embedder=embedder)
            for chunk, vector in zip(missing, vectors, strict=True):
                chunk.embedding = vector
        self.store.setdefault(document_id, []).extend(chunks)

    def chunks(self, document_id: str) -> list[Chunk]:
        return self.store.get(document_id, [])


_SQL = "SELECT id::text, block_ids::text[], text, embedding::text FROM {table} WHERE document_id = %(document_id)s"


class PgVectorSearch:
    """Фрагменты из PostgreSQL. Соединение — psycopg 3 или строка подключения."""

    def __init__(self, connection=None, *, dsn: str | None = None, table: str = "chunk"):
        if not table.replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"Недопустимое имя таблицы: {table}")
        self.connection = connection
        self.dsn = dsn
        self.sql = _SQL.format(table=table)

    def _connect(self):
        if self.connection is not None:
            return self.connection, False
        import psycopg

        return psycopg.connect(self.dsn), True

    def chunks(self, document_id: str) -> list[Chunk]:
        conn, own = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(self.sql, {"document_id": document_id})
                rows = cur.fetchall()
        finally:
            if own:
                conn.close()
        return [Chunk(id=str(r[0]), block_ids=[str(b) for b in r[1]], text=r[2], embedding=_parse_vector(r[3]))
                for r in rows]


def _parse_vector(value) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",") if x]
    return [float(x) for x in value]


# --- Лексический поиск -------------------------------------------------------------------

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_STOP = frozenset(
    "и в во не что на с со как а то все так его но да к у же за бы по только ее от о из при для "
    "или ли если уже до без под над об это этот эти тот том того чем при через после между также "
    "который которая которые которого либо иной иные иных".split()
)
_STEM = 5
"""Основа слова — первые пять букв: для русского поиска это грубо, но без словарей и не хуже
стеммера на терминах закупок («обеспечение», «обеспечения» → «обесп»)."""


def lexical_terms(text: str) -> list[str]:
    out = []
    for word in _WORD.findall(text.lower().replace("ё", "е")):
        if word in _STOP or (len(word) < 2 and not word.isdigit()):
            continue
        out.append(word[:_STEM])
    return out


class LexicalIndex:
    """BM25 по фрагментам одного документа."""

    def __init__(self, texts: Sequence[str], *, k1: float = 1.2, b: float = 0.75):
        self.docs = [Counter(lexical_terms(t)) for t in texts]
        self.lengths = [sum(c.values()) for c in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df = Counter(term for doc in self.docs for term in doc)
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        terms = set(lexical_terms(query)) & self.idf.keys()
        out = []
        for doc, length in zip(self.docs, self.lengths, strict=True):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * length / self.avg) if self.avg else self.k1
            for term in terms:
                f = doc.get(term)
                if f:
                    score += self.idf[term] * f * (self.k1 + 1) / (f + norm)
            out.append(score)
        return out


# --- Ранжирование и отбор ----------------------------------------------------------------


def _cosines(vector: Sequence[float], chunks: Sequence[Chunk]) -> list[float]:
    """Косинус запроса с каждым фрагментом; фрагмент без эмбеддинга получает 0."""
    import numpy as np

    q = np.asarray(vector, dtype=np.float32)
    qn = float(np.linalg.norm(q)) or 1.0
    out = [0.0] * len(chunks)
    rows = [i for i, c in enumerate(chunks) if c.embedding is not None and len(c.embedding) == len(q)]
    if rows:
        m = np.asarray([chunks[i].embedding for i in rows], dtype=np.float32)
        norms = np.linalg.norm(m, axis=1)
        norms[norms == 0] = 1.0
        for i, value in zip(rows, (m @ q) / (norms * qn), strict=True):
            out[i] = float(value)
    return out


def _order(scores: Sequence[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: -scores[i])


def rank_chunks(
    chunks: Sequence[Chunk],
    vector: Sequence[float] | None,
    query_text: str | None,
    *,
    mode: SearchMode | None = None,
    lexical: LexicalIndex | None = None,
) -> list[ChunkHit]:
    """Все фрагменты документа по убыванию близости к запросу.

    `score` в гибриде — сумма RRF двух рангов, в остальных режимах — своя оценка режима.
    `lexical` можно построить один раз на документ и передать для всех правил группы.
    """
    if not chunks:
        return []
    mode = mode or get_settings().search_mode
    if mode not in ("hybrid", "dense", "lexical"):
        raise ValueError(f"SEARCH_MODE={mode}: ожидается hybrid, dense или lexical")
    dense = _cosines(vector, chunks) if vector is not None else None
    lex = None
    if query_text and mode != "dense":
        lex = (lexical or LexicalIndex([c.text for c in chunks])).scores(query_text)
    if mode == "hybrid" and dense is not None and lex is not None:
        fused = [0.0] * len(chunks)
        for ranking in (_order(dense), _order(lex)):
            for pos, i in enumerate(ranking):
                fused[i] += 1 / (RRF_K + pos + 1)
        scores = fused
    elif dense is not None and (mode == "dense" or lex is None):
        scores = dense
    else:
        scores = lex or [0.0] * len(chunks)
    return [ChunkHit(chunks[i].id, list(chunks[i].block_ids), round(scores[i], 6), chunks[i].text)
            for i in _order(scores)]


def take_budget(hits: Sequence[ChunkHit], budget_tokens: int) -> list[ChunkHit]:
    """Первые фрагменты, которые укладываются в бюджет токенов; первый берётся всегда."""
    out: list[ChunkHit] = []
    used = 0
    for hit in hits:
        tokens = estimate_tokens(hit.text)
        if out and used + tokens > budget_tokens:
            break
        out.append(hit)
        used += tokens
    return out


def search_chunks(
    document_id: str,
    query: str,
    k: int | None = None,
    *,
    search: ChunkSearch,
    embedder: Embedder | None = None,
    budget_tokens: int | None = None,
) -> list[ChunkHit]:
    """Фрагменты документа по строке запроса: первые `k` или в пределах бюджета токенов."""
    chunks = search.chunks(document_id)
    vector = embed_texts([query], "query", embedder=embedder)[0] if get_settings().search_mode != "lexical" else None
    hits = rank_chunks(chunks, vector, query)
    if k is not None:
        return hits[:k]
    return take_budget(hits, budget_tokens or get_settings().search_budget_tokens)
