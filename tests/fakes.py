"""Подделки для тестов анализа: эмбеддер без модели и LLM без сети."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable

import httpx

from ml.analysis.llm import LLMClient
from ml.config import Settings

_WORD = re.compile(r"\w+", re.UNICODE)


class FakeEmbedder:
    """Мешок «основ» слов (первые 5 букв), захешированный в 64 измерения."""

    dim = 64

    def embed(self, texts, kind="passage"):
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in _WORD.findall(text.lower()):
                stem = word[:5]
                h = int(hashlib.md5(stem.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def _fragment_of(prompt: str, quote: str) -> int | None:
    pos = prompt.find(quote)
    if pos < 0:
        return None
    headers = [(m.start(), int(m.group(1))) for m in re.finditer(r"^\[(\d+)\]", prompt, re.M)]
    found = None
    for start, index in headers:
        if start <= pos:
            found = index
    return found


class FakeLLM:
    """Отвечает по плану: правило → список (цитата, пояснение). Цитата попадает в ответ,
    только если она есть в тексте запроса — как у честной модели."""

    def __init__(self, plan: dict[str, list[tuple[str, str]]] | None = None, *, invent: dict | None = None,
                 delay: float = 0.0):
        self.plan = plan or {}
        self.invent = invent or {}
        self.delay = delay
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.delay:
            time.sleep(self.delay)
        prompt = body["messages"][-1]["content"]
        if body.get("response_format", {}).get("json_schema", {}).get("name") == "batch_meta":
            m = re.search(r"Наименование объекта закупки:\s*([^\n.]+)", prompt)
            content = json.dumps({"subject": m.group(1) if m else None}, ensure_ascii=False)
            return _ok(content)
        codes = re.findall(r"^### (R-\d+)\.", prompt, re.M)
        rules = []
        for code in codes:
            findings = []
            for quote, explanation in self.plan.get(code, []):
                fragment = _fragment_of(prompt, quote)
                if fragment is not None:
                    findings.append({"fragment": fragment, "quote": quote, "explanation": explanation,
                                     "confidence": 0.9})
            for quote in self.invent.get(code, []):
                findings.append({"fragment": 1, "quote": quote, "explanation": "выдумано", "confidence": 0.7})
            rules.append({"rule_code": code, "findings": findings})
        return _ok(json.dumps({"rules": rules}, ensure_ascii=False))

    def client(self, **settings) -> LLMClient:
        return make_client(self.handler, **settings)


def _ok(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0001},
    })


def make_client(handler: Callable[[httpx.Request], httpx.Response], **overrides) -> LLMClient:
    values = {"llm_model": "test/model", "llm_api_key": "test-key", "llm_max_retries": 2, "llm_cache": False}
    values.update(overrides)
    settings = Settings(**values)
    return LLMClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
