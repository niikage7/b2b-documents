"""Адаптер LLM с единым интерфейсом (FR-4.1): OpenRouter или любой API, совместимый с OpenAI.

- таймауты и повторы с экспоненциальной паузой (429, 5xx, обрыв соединения);
- не больше `LLM_MAX_CONCURRENCY` (по умолчанию 8) одновременных вызовов на процесс;
- структурированный JSON: `json_schema`, если модель его не принимает — `json_object`,
  затем обычный текст; ответ всегда проверяется по схеме Pydantic, неверный ответ
  один раз отправляется модели на исправление;
- учёт токенов (в том числе рассуждений) и стоимости, кеш ответов на диске;
- рассуждения модели задаются `LLM_REASONING` (параметр `reasoning.effort` OpenRouter):
  они входят в `max_tokens` и занимают большую часть времени ответа. Модель, которая
  параметр не принимает, дальше вызывается без него.

Локальная модель (vLLM) подключается сменой `LLM_BASE_URL`, `LLM_MODEL` и `LLM_API_KEY`;
`LLM_REASONING` ей не передаётся.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from contracts.analysis import LLMUsage
from ml.analysis.cache import DirCache, cache_key
from ml.config import Settings, get_settings
from ml.errors import LLMError, LLMResponseError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
_FORMATS = ("json_schema", "json_object", "none")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

_semaphores: dict[int, threading.BoundedSemaphore] = {}
_semaphore_lock = threading.Lock()
# Модели, которые отказались от json_schema / json_object: дальше сразу шлём более простой формат
_format_downgrades: dict[str, str] = {}
# Модели, которые отказались от параметра reasoning (например, рассуждают всегда): дальше без него
_reasoning_rejected: set[str] = set()


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _semaphore_lock:
        if limit not in _semaphores:
            _semaphores[limit] = threading.BoundedSemaphore(limit)
        return _semaphores[limit]


@dataclass
class LLMResult:
    data: BaseModel
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: str = ""
    cached: bool = False


def _strict_schema(schema: dict) -> dict:
    """JSON Schema для режима strict: у каждого объекта все поля обязательны и нет лишних."""
    defs = schema.pop("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(dict(defs[node["$ref"].split("/")[-1]]))
            drop = ("title", "default", "description", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
            node = {k: fix(v) for k, v in node.items() if k not in drop}
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            if "anyOf" in node:
                node["anyOf"] = [fix(x) for x in node["anyOf"]]
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(schema)


def parse_json(text: str) -> object:
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        http_client: httpx.Client | None = None,
        cache: DirCache | None = None,
    ):
        self.settings = settings or get_settings()
        self.model = model or self.settings.llm_model
        self.base_url = self.settings.llm_base_url.rstrip("/")
        self.openrouter = "openrouter.ai" in self.base_url
        self.http = http_client or httpx.Client(timeout=self.settings.llm_timeout)
        if cache is None and self.settings.llm_cache:
            cache = DirCache(self.settings.cache_dir / "llm")
        self.cache = cache
        self.reasoning = reasoning_effort(self.settings.llm_reasoning)
        if self.reasoning and not self.openrouter:
            log.info("llm: LLM_REASONING=%s передаётся только OpenRouter, для %s не используется",
                        self.reasoning, self.base_url)
            self.reasoning = None

    @property
    def reasoning_active(self) -> str | None:
        """Режим рассуждений, который реально уходит в запросах: None, если модель его отвергла."""
        return None if self.model in _reasoning_rejected else self.reasoning

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        if self.openrouter:
            headers["X-Title"] = "B2B Tender"
        return headers

    def _payload(self, messages: list[dict], schema_name: str, schema: dict, fmt: str, max_tokens: int,
                 reasoning: str | None = None) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens,
        }
        if reasoning:
            # Текст рассуждений не нужен: exclude убирает его из ответа, но токены всё равно оплачиваются
            payload["reasoning"] = {"effort": reasoning} if reasoning == "none" else {
                "effort": reasoning, "exclude": True}
        if fmt == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif fmt == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.openrouter:
            payload["usage"] = {"include": True}
            if fmt != "none":
                # Только провайдеры, которые поддерживают response_format
                payload["provider"] = {"require_parameters": True}
        return payload

    def _post(self, payload: dict) -> dict:
        settings = self.settings
        attempts = settings.llm_max_retries + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with _semaphore(settings.llm_max_concurrency):
                    response = self.http.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                        timeout=settings.llm_timeout,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                log.warning("llm: %s, попытка %d из %d", type(exc).__name__, attempt + 1, attempts)
            else:
                if response.status_code == 200:
                    body = response.json()
                    if body.get("error"):
                        last = LLMError(detail=str(body["error"])[:300])
                    else:
                        return body
                elif response.status_code in _RETRY_STATUS:
                    last = LLMError(detail=f"HTTP {response.status_code}: {response.text[:300]}")
                    log.warning("llm: HTTP %d, попытка %d из %d", response.status_code, attempt + 1, attempts)
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit() and attempt + 1 < attempts:
                        time.sleep(min(float(retry_after), 30))
                        continue
                else:
                    raise _client_error(response)
            if attempt + 1 < attempts:
                time.sleep(min(20.0, 2**attempt) * (0.5 + random.random()))
        raise LLMError(detail=str(last)) from last

    def chat(
        self,
        messages: list[dict],
        response_model: type[T],
        *,
        schema_name: str = "answer",
        max_tokens: int | None = None,
        reasoning: str | None = None,
    ) -> LLMResult:
        """Вызов модели с ответом по схеме `response_model`.

        `reasoning` заменяет `LLM_REASONING` для этого вызова: короткому ответу с малым
        `max_tokens` рассуждения не нужны, а с ними он может прийти пустым.
        """
        if not self.model:
            raise LLMError("Модель ИИ не настроена", detail="пустой LLM_MODEL")
        max_tokens = max_tokens or self.settings.llm_max_tokens
        effort = self.reasoning
        if reasoning is not None:
            effort = reasoning_effort(reasoning) if self.openrouter else None
        schema = _strict_schema(response_model.model_json_schema())
        key_parts = {"model": self.model, "messages": messages, "schema": schema_name,
                     "temperature": self.settings.llm_temperature}
        if effort:
            # Ответы с рассуждениями и без них кешируются отдельно, иначе замер сравнит кеш с кешем
            key_parts["reasoning"] = effort
        key = cache_key(key_parts)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                try:
                    data = response_model.model_validate(hit["data"])
                    return LLMResult(data, LLMUsage(cached_calls=1), hit.get("raw", ""), cached=True)
                except ValidationError:
                    pass

        usage = LLMUsage()
        fmt = _format_downgrades.get(self.model, self.settings.llm_response_format)
        if fmt not in _FORMATS:
            fmt = "json_schema"
        conversation = list(messages)
        repaired = False
        while True:
            active = None if self.model in _reasoning_rejected else effort
            try:
                body = self._post(self._payload(conversation, schema_name, schema, fmt, max_tokens, active))
            except _ParamRejected as exc:
                if active and exc.about in ("reasoning", "parameter"):
                    # Сначала убираем reasoning: формат ответа важнее для разбора находок
                    log.warning("llm: %s не принимает reasoning=%s, дальше без него: %s",
                                self.model, active, exc.detail)
                    _reasoning_rejected.add(self.model)
                    continue
                if exc.about == "reasoning":
                    raise LLMError(detail=exc.detail) from None
                next_fmt = _FORMATS[_FORMATS.index(fmt) + 1] if fmt != "none" else None
                if next_fmt is None:
                    raise LLMError(detail="модель не принимает запрос") from None
                log.info("llm: %s не принимает %s, перехожу на %s", self.model, fmt, next_fmt)
                _format_downgrades[self.model] = next_fmt
                fmt = next_fmt
                continue
            _add_usage(usage, body)
            if _finish_reason(body) == "length":
                log.warning("llm: ответ %s обрезан по max_tokens=%d (рассуждения входят в этот лимит)",
                            self.model, max_tokens)
            raw = _content(body)
            try:
                data = response_model.model_validate(parse_json(raw))
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                if repaired:
                    raise LLMResponseError(detail=f"{exc}"[:300]) from exc
                repaired = True
                log.warning("llm: ответ не по схеме, прошу исправить: %s", str(exc)[:200])
                conversation = [
                    *messages,
                    {"role": "assistant", "content": raw[:6000]},
                    {"role": "user", "content": f"Ответ не соответствует схеме JSON: {str(exc)[:500]}. "
                                                "Верни исправленный ответ — только JSON по схеме, без пояснений."},
                ]
                continue
            if self.cache is not None:
                self.cache.set(key, {"data": data.model_dump(mode="json"), "raw": raw})
            return LLMResult(data, usage, raw)


class _ParamRejected(LLMError):
    """Модель или провайдер не принимает параметр запроса. `about` — какой: `reasoning`,
    `format` (response_format) или `parameter` (из ответа не понять)."""

    def __init__(self, about: str, *, detail: str):
        self.about = about
        super().__init__(detail=detail)


def reasoning_effort(value: str) -> str | None:
    """Значение `LLM_REASONING`: None — параметр не передаётся, модель рассуждает как по умолчанию."""
    effort = value.strip().lower()
    if effort in ("", "default"):
        return None
    if effort not in REASONING_EFFORTS:
        raise LLMError("Настройка рассуждений модели ИИ задана неверно",
                       detail=f"LLM_REASONING={value!r}, допустимо: default, {', '.join(REASONING_EFFORTS)}")
    return effort


def _client_error(response: httpx.Response) -> LLMError:
    text = response.text[:500]
    if response.status_code in (400, 404, 422):
        detail = f"HTTP {response.status_code}: {text}"
        if re.search(r"reasoning|thinking", text, re.I):
            return _ParamRejected("reasoning", detail=detail)
        if re.search(r"response_format|json_schema|structured", text, re.I):
            return _ParamRejected("format", detail=detail)
        if re.search(r"parameter", text, re.I):
            return _ParamRejected("parameter", detail=detail)
    if response.status_code in (401, 403):
        return LLMError("Нет доступа к сервису проверки ИИ: проверьте ключ API", detail=text)
    return LLMError(detail=f"HTTP {response.status_code}: {text}")


def _content(body: dict) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMResponseError(detail=f"нет choices: {str(body)[:200]}") from exc
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content and message.get("parsed") is not None:
        return json.dumps(message["parsed"], ensure_ascii=False)
    return content or ""


def _finish_reason(body: dict) -> str | None:
    try:
        return body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _add_usage(usage: LLMUsage, body: dict) -> None:
    u = body.get("usage") or {}
    usage.calls += 1
    usage.prompt_tokens += int(u.get("prompt_tokens") or 0)
    usage.completion_tokens += int(u.get("completion_tokens") or 0)
    usage.reasoning_tokens += int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    usage.cost += float(u.get("cost") or 0)


_default_client: LLMClient | None = None
_default_lock = threading.Lock()


def default_client() -> LLMClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = LLMClient()
        return _default_client
