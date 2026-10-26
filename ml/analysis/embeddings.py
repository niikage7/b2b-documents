"""Эмбеддинги текста (bi-encoder) для фрагментов и строк поиска правил.

Модель задаётся в `.env` (`EMBEDDING_MODEL`), размерность сверяется с `EMBEDDING_DIM`:
от неё зависит `vector(768)` в схеме. Веса скачиваются при сборке образа, в рантайме
модель грузится один раз на процесс.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Literal, Protocol

from contracts.analysis import RuleSpec
from ml.analysis.templates import rule_query_text
from ml.config import get_settings

log = logging.getLogger(__name__)

TextKind = Literal["query", "passage"]

# Префиксы, с которыми модели обучали: без них качество поиска заметно падает
_PREFIXES: list[tuple[str, tuple[str, str]]] = [
    ("e5", ("query: ", "passage: ")),
    ("frida", ("search_query: ", "search_document: ")),
    ("berta", ("search_query: ", "search_document: ")),
    ("giga-embeddings", ("Instruct: Найди фрагмент договора по описанию риска\nQuery: ", "")),
]


def prefixes(model: str) -> tuple[str, str]:
    name = model.lower()
    for key, value in _PREFIXES:
        if key in name:
            return value
    return "", ""


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str], kind: TextKind = "passage") -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Обёртка sentence-transformers: нормированные векторы, батчинг, префиксы модели."""

    def __init__(self, model: str | None = None, *, device: str | None = None, batch_size: int | None = None,
                 dim: int | None = None):
        settings = get_settings()
        self.model_name = model or settings.embedding_model
        self.device = device or settings.embedding_device
        self.batch_size = batch_size or settings.embedding_batch_size
        self.query_prefix, self.passage_prefix = prefixes(self.model_name)
        self._model = None
        self._lock = threading.Lock()
        self.dim = dim or settings.embedding_dim

    def _load(self):
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name, device=self.device)
                get_dim = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
                dim = get_dim()
                if dim != self.dim:
                    raise ValueError(f"Модель {self.model_name} даёт векторы {dim}, а в схеме vector({self.dim})")
                log.info("embeddings: загружена %s (%d)", self.model_name, dim)
        return self._model

    def embed(self, texts: Sequence[str], kind: TextKind = "passage") -> list[list[float]]:
        if not texts:
            return []
        prefix = self.query_prefix if kind == "query" else self.passage_prefix
        model = self._load()
        vectors = model.encode(
            [prefix + t for t in texts],
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.astype("float32").tolist()


_default: Embedder | None = None
_default_lock = threading.Lock()


def default_embedder() -> Embedder:
    global _default
    with _default_lock:
        if _default is None:
            _default = SentenceTransformerEmbedder()
        return _default


def set_default_embedder(embedder: Embedder | None) -> None:
    """Подменяет модель (тесты, стенды, другая модель без перезапуска)."""
    global _default
    with _default_lock:
        _default = embedder


def embed_texts(texts: Sequence[str], kind: TextKind = "passage", *, embedder: Embedder | None = None) -> list[list[float]]:
    """Векторы для фрагментов (`kind="passage"`, этап `index_chunks`) или запросов (`"query"`)."""
    return (embedder or default_embedder()).embed(list(texts), kind)


def embed_rule_query(rule: RuleSpec, *, embedder: Embedder | None = None) -> list[float]:
    """Эмбеддинг строки поиска правила: для `rule_version.query_embedding` при сохранении."""
    return embed_texts([rule_query_text(rule)], "query", embedder=embedder)[0]
