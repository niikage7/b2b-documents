"""Заглушка LLM для разработки: сервис, совместимый с OpenAI `POST /v1/chat/completions`.

Конвейер и тест правила в docker-compose ходят сюда вместо OpenRouter (`LLM_BASE_URL`), поэтому
разработка и проверки ничего не стоят. Ответ строится по имени JSON-схемы из запроса без модели:

- `risk_findings` (группа правил) — для каждого правила ищется предложение из его фрагментов, где
  больше всего слов из названия и сути риска (слова названия весят вдвое, число в предложении —
  плюс балл). Нашлось — находка с дословной цитатой, нет — «нет риска» (пустой список);
- `finding_verdict` (второй взгляд на находку) — находка всегда подтверждается;
- `batch_meta` — предмет закупки по строке «Наименование объекта закупки: …»;
- `procurement_passport` — поля паспорта по ключевым словам («срок поставки», «обеспечение исполнения»);
- `clarification_request` — по вопросу на каждую находку.

Цитаты всегда берутся из текста запроса, как у честной модели, поэтому проходят привязку к блокам.
Качество находок здесь не проверяется — только то, что конвейер доходит до находок, цветов и счётчиков.

Только стандартная библиотека Python. Запуск: `python devtools/llm_stub/server.py`
(порт `LLM_STUB_PORT`, по умолчанию 8080; `LLM_STUB_DELAY` — задержка ответа в секундах).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("llm_stub")

MODEL = "stub/b2b-llm-stub"
MAX_QUOTE = 280
EXPLANATION = ("Ответ заглушки LLM для разработки: условие похоже на риск по правилу «{title}». "
               "Настоящая модель не вызывалась.")
RECOMMENDATION = "Проверьте условие вручную: это ответ заглушки, а не модели."

_WORD = re.compile(r"\w+", re.UNICODE)
_NUMBER_PREFIX = re.compile(r"^\s*(?:\d{1,3}(?:\.\d{1,3})*\.?|[а-яa-z]\))\s+", re.IGNORECASE)
# Конец предложения, но не сокращение из одной буквы («г. Москва», «т. е.»)
_SENTENCE = re.compile(r"(?<![\s(]\w\.)(?<=[.;!?])\s+|\s*…\s*")
_FRAGMENT = re.compile(r"^\[(\d+)\][^\n]*$", re.M)
_RULE = re.compile(r"^### (\S+?)\.\s+(.*)$", re.M)
_REFS = re.compile(r"\[(\d+)\]")
# Слова, которые есть почти в любом правиле и любом пункте договора: по ним предложение не выбирается
_STOP = {"поста", "заказ", "контр", "догов", "докум", "услов", "котор", "более", "менее", "может", "этого",
         "риска", "закуп", "товар", "срока", "сроки", "также", "между", "после", "свыше", "чтобы",
         "своих", "участ"}


def _stems(text: str) -> set[str]:
    """Основы слов: первые 5 букв («неустойка» → «неуст»), короткие слова («срок», «пени») целиком."""
    return {w[:5] for w in (m.group(0).lower().replace("ё", "е") for m in _WORD.finditer(text))
            if len(w) >= 4 and not w.isdigit() and w[:5] not in _STOP}


def _seed(*parts: str) -> int:
    return int(hashlib.md5("|".join(parts).encode()).hexdigest()[:8], 16)


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = _NUMBER_PREFIX.sub("", line).strip()
        for part in _SENTENCE.split(line):
            part = part.strip(" …")
            if len(part) >= 12:
                out.append(_cut(part))
    return out


def _cut(text: str) -> str:
    if len(text) <= MAX_QUOTE:
        return text
    return text[:MAX_QUOTE].rsplit(" ", 1)[0]


def _fragments(text: str) -> dict[int, str]:
    """`[N] стр. 3, раздел «…»` и строки под ним до следующего фрагмента."""
    marks = list(_FRAGMENT.finditer(text))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.end():end].strip()
    return out


# --- Группа правил ----------------------------------------------------------------------------


def risk_findings(prompt: str) -> dict:
    head, _, rules_part = prompt.partition("Правила проверки:")
    fragments = _fragments(head.partition("Фрагменты документа:")[2] or head)
    marks = list(_RULE.finditer(rules_part))
    answers: list[dict] = []
    used: set[str] = set()
    for i, m in enumerate(marks):
        code, title = m.group(1), m.group(2).strip()
        body = rules_part[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(rules_part)]
        where = re.search(r"^Где искать:(.*)$", body, re.M)
        refs = [int(r) for r in _REFS.findall(where.group(1))] if where else []
        essence = re.search(r"^Суть риска:(.*)$", body, re.M)
        found = _rule_findings(code, title, essence.group(1) if essence else "", fragments, refs, used)
        answers.append({"rule_code": code, "findings": found})
    return {"rules": answers}


MIN_SCORE = 3


def _rule_findings(code: str, title: str, essence: str, fragments: dict[int, str], refs: list[int],
                   used: set[str]) -> list[dict]:
    """Одно предложение с наибольшим баллом; предложение, уже ставшее находкой другого правила, не берётся."""
    title_keys, essence_keys = _stems(title), _stems(essence)
    best: tuple[int, int, str] | None = None
    for index in refs or sorted(fragments):
        for sentence in _sentences(fragments.get(index, "")):
            if sentence in used:
                continue
            stems = _stems(sentence)
            score = 2 * len(title_keys & stems) + len(essence_keys & stems) + bool(re.search(r"\d", sentence))
            if score >= MIN_SCORE and (best is None or score > best[0]):
                best = (score, index, sentence)
    if best is None:
        return []
    _, index, quote = best
    used.add(quote)
    return [{
        "fragment": index, "quote": quote, "check": f"условие сравнивается с правилом {code}", "risk": True,
        "explanation": EXPLANATION.format(title=title), "recommendation": RECOMMENDATION,
        "confidence": round(0.6 + _seed(code, quote) % 35 / 100, 2),
    }]


# --- Предмет закупки и паспорт ----------------------------------------------------------------

_SUBJECT = re.compile(r"(?:наименование\s+объекта\s+закупки|предмет\s+(?:контракта|договора|закупки))"
                      r"\s*[:\-–—]\s*([^\n]{5,300}?)(?:\.\s|\.?$|\n)", re.IGNORECASE | re.M)

# Поле паспорта → варианты: все подстроки варианта есть в предложении (без учёта регистра и «ё»),
# подстроки с «!» — их в предложении быть не должно
PASSPORT_KEYS: dict[str, list[tuple[str, ...]]] = {
    "subject": [("наименование объекта закупки",), ("предмет контракта",)],
    "number": [("номер извещения",), ("реестровый номер",), ("аукциона №",)],
    "procedure": [("способ определения поставщика",), ("электронный аукцион",)],
    "customer": [("наименование заказчика",), ("заказчик:",)],
    "nmck": [("начальная (максимальная) цена",), ("цена контракта составляет",)],
    "application_security": [("обеспечени", "заявк")],
    "contract_security": [("обеспечени", "исполнени")],
    "warranty_security": [("обеспечени", "гарантийн")],
    "advance": [("аванс",)],
    "delivery_term": [("срок поставки",), ("поставк", "в течение")],
    "delivery_place": [("место поставки",), ("адрес поставки",)],
    "acceptance_term": [("приемк", "в течение", "!оплат")],
    "payment_term": [("оплат", "в течение")],
    "warranty_term": [("гарантийный срок",)],
    "deadline": [("окончания срока подачи заявок",), ("окончание подачи заявок",)],
}


def batch_meta(prompt: str) -> dict:
    m = _SUBJECT.search(prompt)
    return {"subject": m.group(1).strip() if m else None}


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _matches(sentence: str, variant: tuple[str, ...]) -> bool:
    text = _norm(sentence)
    return all((part[1:] not in text) if part.startswith("!") else (part in text) for part in variant)


def _has_value(sentence: str) -> bool:
    """Значение, а не заголовок раздела: есть число или текст после двоеточия."""
    return bool(re.search(r"\d", sentence)) or bool(sentence.partition(":")[2].strip(" ."))


def _value(sentence: str) -> str:
    for sep in (":", "составляет"):
        head, found, tail = sentence.partition(sep)
        if found and tail.strip():
            return tail.strip(" .;")[:120]
    return sentence[:120]


def procurement_passport(prompt: str) -> dict:
    fragments_part, _, fields_part = prompt.partition("Поля паспорта:")
    fragments = _fragments(fragments_part)
    wanted = re.findall(r"^- (\w+) — ", fields_part, re.M)
    fields = []
    for key in wanted:
        variants = PASSPORT_KEYS.get(key, [])
        found = next(((i, s) for i, text in sorted(fragments.items()) for s in _sentences(text)
                      if _has_value(s) and any(_matches(s, v) for v in variants)), None)
        if found:
            index, sentence = found
            fields.append({"key": key, "value": _value(sentence), "fragment": index, "quote": sentence})
    return {"fields": fields}


def clarification_request(prompt: str) -> dict:
    n = len(re.findall(r"^\[(\d+)\]", prompt, re.M))
    return {"items": [{"index": i, "request": f"Просим разъяснить условие {i}."} for i in range(1, n + 1)]}


# --- Протокол OpenAI --------------------------------------------------------------------------

def finding_verdict(prompt: str) -> dict:
    """Второй взгляд на находку: заглушка подтверждает всё, что нашла сама."""
    return {"check": "Условие подтверждено заглушкой", "risk": True}


ANSWERS = {
    "risk_findings": risk_findings,
    "finding_verdict": finding_verdict,
    "batch_meta": batch_meta,
    "procurement_passport": procurement_passport,
    "clarification_request": clarification_request,
}


def schema_name(body: dict, prompt: str) -> str | None:
    name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name")
    if name:
        return name
    # Формат json_object или без формата: схема угадывается по тексту запроса
    if "Правила проверки:" in prompt:
        return "risk_findings"
    if "Проверяемое условие:" in prompt:
        return "finding_verdict"
    if "Поля паспорта:" in prompt:
        return "procurement_passport"
    if '"subject"' in prompt:
        return "batch_meta"
    return None


def respond(body: dict) -> dict:
    """Тело ответа `chat/completions` на тело запроса."""
    messages = body.get("messages") or []
    prompt = str(messages[-1].get("content", "")) if messages else ""
    name = schema_name(body, prompt)
    answer = ANSWERS[name](prompt) if name in ANSWERS else {}
    content = json.dumps(answer, ensure_ascii=False)
    prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
    return {
        "id": f"chatcmpl-stub-{_seed(prompt, content):08x}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(content) // 4,
                  "total_tokens": prompt_tokens + len(content) // 4, "cost": 0.0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "b2b-llm-stub/1"
    delay = float(os.environ.get("LLM_STUB_DELAY") or 0)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — имя метода задаёт http.server
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._send(200, {"status": "ok"})
        elif self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "b2b"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        started = time.monotonic()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "request body is not JSON"}})
            return
        if self.delay:
            time.sleep(self.delay)
        payload = respond(body)
        self._send(200, payload)
        log.info("chat/completions: %s, %d мс", schema_name(body, str((body.get("messages") or [{}])[-1].get(
            "content", ""))), int((time.monotonic() - started) * 1000))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — сигнатура http.server
        log.debug(format, *args)


def serve(port: int | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port if port is not None else int(os.environ.get("LLM_STUB_PORT") or 8080)),
                               Handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = serve()
    log.info("Заглушка LLM слушает порт %d", server.server_address[1])
    server.serve_forever()
