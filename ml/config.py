"""Настройки ML-части из переменных окружения (`.env`).

Все пороги, которые влияют на результат, собраны здесь, чтобы их было видно в одном месте.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PIPELINE_VERSION = "2026.10.28-1"
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
        # Выбрана 27.10 на стенде bench.embeddings: на фрагментах по 60 токенов e5-base, BERTA и
        # USER2-base равны (0,99–1,00 при бюджете 1600), при бюджете 800 BERTA точнее векторным
        # поиском (0,95 против 0,90 и 0,88) и вдвое легче e5-base
        default_factory=lambda: _env("EMBEDDING_MODEL", "sergeyzh/BERTA")
    )
    embedding_dim: int = field(default_factory=lambda: _env_int("EMBEDDING_DIM", 768))
    embedding_batch_size: int = field(default_factory=lambda: _env_int("EMBEDDING_BATCH_SIZE", 32))
    embedding_device: str = field(default_factory=lambda: _env("EMBEDDING_DEVICE", "cpu"))

    # LLM (FR-4.1): OpenRouter или любой API, совместимый с OpenAI (локальный vLLM)
    llm_base_url: str = field(
        default_factory=lambda: _env("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    )
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    # Модель по умолчанию выбрана сравнением 27.10 (docs/ml-plan.md): та же точность, что у v4-flash-0731,
    # полнота на трудных пунктах 0,93 против 0,78, 20 страниц — 8 с против 10 с
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek/deepseek-v4.1-flash-1017"))
    llm_meta_model: str = field(default_factory=lambda: _env("LLM_META_MODEL", ""))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 90))
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 3))
    llm_max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 8))
    llm_response_format: str = field(
        default_factory=lambda: _env("LLM_RESPONSE_FORMAT", "json_schema")
    )
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))
    # Предел ответа, а не его длина: оплачиваются только выданные токены. 4000 не хватало плотному
    # документу (90 пунктов с риском на 20 страниц): с вердиктом `check`/`risk` ответ группы обрезался.
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 8000))
    # Рассуждения модели (только OpenRouter): `default` — как у модели, `none` — выключить,
    # `minimal` … `max` — уровень. Токены рассуждений входят в LLM_MAX_TOKENS и в стоимость.
    # По умолчанию `none`: на deepseek-v4.1-flash-1017 анализ 20 страниц — 8 с против 15 с с `low`,
    # полнота 9 из 10 у обоих, на трудных пунктах 0,93 против 0,97. С `low` ответы обрезались по
    # max_tokens, у v4-flash-0731 группа правил падала целиком; с уровнем модели (high) — 3 прогона из 4 (замеры 27.10).
    llm_reasoning: str = field(default_factory=lambda: _env("LLM_REASONING", "none"))
    llm_cache: bool = field(default_factory=lambda: _env("LLM_CACHE", "1") not in ("0", "false", ""))

    # Движок правил
    rules_per_group: int = field(default_factory=lambda: _env_int("RULES_PER_GROUP", 4))
    # Поиск фрагментов для semantic_llm (docs/ml-plan.md, «Поиск фрагментов 27.10»): фрагменты по
    # CHUNK_TOKENS (около одного пункта договора), гибрид векторов и BM25, каждому правилу — до
    # SEARCH_BUDGET_TOKENS текста. На стенде с рассыпанными пунктами: 0,99–1,00 пунктов с риском против 0,60.
    # Бюджет 3200 — столько же, сколько давали прежние top-8 фрагментов по 400 токенов
    chunk_tokens: int = field(default_factory=lambda: _env_int("CHUNK_TOKENS", 60))
    chunk_overlap: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP", 0))
    search_mode: str = field(default_factory=lambda: _env("SEARCH_MODE", "hybrid"))
    search_budget_tokens: int = field(default_factory=lambda: _env_int("SEARCH_BUDGET_TOKENS", 3200))
    llm_window_tokens: int = field(default_factory=lambda: _env_int("LLM_WINDOW_TOKENS", 6000))
    quote_min_score: float = field(default_factory=lambda: _env_float("QUOTE_MIN_SCORE", 90))
    test_rule_timeout: float = field(default_factory=lambda: _env_float("TEST_RULE_TIMEOUT", 60))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
