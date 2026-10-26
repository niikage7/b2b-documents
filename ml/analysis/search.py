"""Поиск фрагментов документа по смыслу: top-k по косинусной близости.

`PgVectorSearch` читает таблицу `chunk` из «Требований к бэкенду» (`id`, `document_id`,
`block_ids`, `text`, `embedding`). Фрагменты одного документа сначала отбираются по
`document_id`, затем сортируются по расстоянию: HNSW с фильтром после обхода графа может
вернуть меньше k строк, а у одного документа фрагментов сотни, полный перебор быстрый.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from ml.analysis.blocks import Chunk, ChunkHit
from ml.analysis.embeddings import Embedder, embed_texts
from ml.config import get_settings


class ChunkSearch(Protocol):
    def search(self, document_id: str, vector: Sequence[float], k: int) -> list[ChunkHit]: ...


class InMemorySearch:
    """Поиск по фрагментам в памяти: `test_rule` по тексту, тесты, стенды."""

    def __init__(self, chunks_by_document: dict[str, list[Chunk]] | None = None):
        self.chunks: dict[str, list[Chunk]] = chunks_by_document or {}

    def add(self, document_id: str, chunks: list[Chunk], *, embedder: Embedder | None = None) -> None:
        missing = [c for c in chunks if c.embedding is None]
        if missing:
            vectors = embed_texts([c.text for c in missing], "passage", embedder=embedder)
            for chunk, vector in zip(missing, vectors, strict=True):
                chunk.embedding = vector
        self.chunks.setdefault(document_id, []).extend(chunks)

    def search(self, document_id: str, vector: Sequence[float], k: int) -> list[ChunkHit]:
        scored = []
        for chunk in self.chunks.get(document_id, []):
            scored.append((_cosine(vector, chunk.embedding or []), chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ChunkHit(c.id, list(c.block_ids), round(s, 4), c.text) for s, c in scored[:k]]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_SQL = """
WITH doc AS MATERIALIZED (
    SELECT id, block_ids, text, embedding FROM {table} WHERE document_id = %(document_id)s
)
SELECT id::text, block_ids::text[], text, 1 - (embedding <=> %(vector)s::vector) AS score
FROM doc
ORDER BY embedding <=> %(vector)s::vector
LIMIT %(k)s
"""


class PgVectorSearch:
    """Поиск по pgvector. Соединение — psycopg 3 или фабрика соединений."""

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

    def search(self, document_id: str, vector: Sequence[float], k: int) -> list[ChunkHit]:
        literal = "[" + ",".join(f"{v:.7g}" for v in vector) + "]"
        conn, own = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(self.sql, {"document_id": document_id, "vector": literal, "k": k})
                rows = cur.fetchall()
        finally:
            if own:
                conn.close()
        return [ChunkHit(str(r[0]), [str(b) for b in r[1]], float(r[3]), r[2]) for r in rows]


def search_chunks(
    document_id: str,
    query: str | Sequence[float],
    k: int | None = None,
    *,
    search: ChunkSearch,
    embedder: Embedder | None = None,
) -> list[ChunkHit]:
    """top-k фрагментов документа по строке запроса или готовому вектору."""
    k = k or get_settings().search_top_k
    vector = embed_texts([query], "query", embedder=embedder)[0] if isinstance(query, str) else list(query)
    return search.search(document_id, vector, k)
