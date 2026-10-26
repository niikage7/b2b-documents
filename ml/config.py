"""Настройки ML-части из переменных окружения (`.env`).

Все пороги, которые влияют на результат, собраны здесь, чтобы их было видно в одном месте.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PIPELINE_VERSION = "2026.10.26-5"
"""Версия конвейера извлечения. Любая правка подготовки или чистки OCR увеличивает её,
иначе кеш молча отдаст старое распознавание."""


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    # Лимиты
    max_pages: int = field(default_factory=lambda: _env_int("ML_MAX_PAGES", 500))

    # Кеши (конвертация, OCR, ответы LLM). В docker-compose — общий том воркеров.
    cache_dir: Path = field(default_factory=lambda: _env_path("ML_CACHE_DIR", "/tmp/b2b-ml-cache"))

    # Конвертация DOC и RTF
    soffice_bin: str = field(default_factory=lambda: _env("SOFFICE_BIN", "soffice"))
    convert_timeout: float = field(default_factory=lambda: _env_float("CONVERT_TIMEOUT", 120))

    # OCR (FR-3.3, FR-3.5)
    tesseract_bin: str = field(default_factory=lambda: _env("TESSERACT_BIN", "tesseract"))
    ocr_lang: str = field(default_factory=lambda: _env("OCR_LANG", "rus+eng"))
    ocr_dpi: int = field(default_factory=lambda: _env_int("OCR_DPI", 300))
    ocr_low_quality_threshold: float = field(
        default_factory=lambda: _env_float("OCR_LOW_QUALITY_THRESHOLD", 0.75)
    )
    ocr_timeout: float = field(default_factory=lambda: _env_float("OCR_PAGE_TIMEOUT", 60))

    # Эмбеддинги
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    )
    embedding_dim: int = field(default_factory=lambda: _env_int("EMBEDDING_DIM", 768))
    embedding_batch_size: int = field(default_factory=lambda: _env_int("EMBEDDING_BATCH_SIZE", 32))
    embedding_device: str = field(default_factory=lambda: _env("EMBEDDING_DEVICE", "cpu"))

    # LLM (FR-4.1): OpenRouter или любой API, совместимый с OpenAI (локальный vLLM)
    llm_base_url: str = field(
        default_factory=lambda: _env("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    )
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))
    llm_meta_model: str = field(default_factory=lambda: _env("LLM_META_MODEL", ""))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 90))
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 3))
    llm_max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 8))
    llm_response_format: str = field(
        default_factory=lambda: _env("LLM_RESPONSE_FORMAT", "json_schema")
    )
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 4000))
    llm_cache: bool = field(default_factory=lambda: _env("LLM_CACHE", "1") not in ("0", "false", ""))

    # Движок правил
    rules_per_group: int = field(default_factory=lambda: _env_int("RULES_PER_GROUP", 4))
    search_top_k: int = field(default_factory=lambda: _env_int("SEARCH_TOP_K", 8))
    llm_window_tokens: int = field(default_factory=lambda: _env_int("LLM_WINDOW_TOKENS", 6000))
    quote_min_score: float = field(default_factory=lambda: _env_float("QUOTE_MIN_SCORE", 90))
    test_rule_timeout: float = field(default_factory=lambda: _env_float("TEST_RULE_TIMEOUT", 60))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
