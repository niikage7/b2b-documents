"""Кеш ответов LLM на диске: «Перезапустить проверку» и «Повторить» не платят дважды.

Ключ — хеш модели, сообщений и параметров запроса; в сообщения входят версия правила
(его текст и переменные) и текст фрагментов. Запись атомарная: воркеры делят каталог.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def cache_key(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class DirCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        os.replace(tmp, path)
