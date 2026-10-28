"""Единый формат ошибок: `{"error": {"code", "message", "details"}}`, текст — по-русски.

Фронтенд показывает `message` как есть, а `details.fields` раскладывает по полям формы.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from db.errors import DataError
from ml.errors import AnalysisError, ExtractionError, MLError, RuleTemplateError

log = logging.getLogger(__name__)


class Unauthorized(DataError):
    status = 401
    code = "unauthorized"
    message = "Сессия завершена, войдите снова"


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details))


_VALIDATION_MESSAGES = {
    "missing": "Обязательное поле",
    "string_too_short": "Слишком короткое значение",
    "string_too_long": "Слишком длинное значение",
    "too_short": "Слишком мало элементов",
    "too_long": "Слишком много элементов",
    "int_parsing": "Нужно целое число",
    "int_type": "Нужно целое число",
    "float_parsing": "Нужно число",
    "float_type": "Нужно число",
    "bool_parsing": "Нужно значение «да» или «нет»",
    "bool_type": "Нужно значение «да» или «нет»",
    "string_type": "Нужна строка",
    "list_type": "Нужен список",
    "dict_type": "Нужен объект",
    "uuid_parsing": "Неверный идентификатор",
    "uuid_type": "Неверный идентификатор",
    "literal_error": "Недопустимое значение",
    "enum": "Недопустимое значение",
    "greater_than": "Значение вне допустимого диапазона",
    "greater_than_equal": "Значение вне допустимого диапазона",
    "less_than": "Значение вне допустимого диапазона",
    "less_than_equal": "Значение вне допустимого диапазона",
    "json_invalid": "Тело запроса не является корректным JSON",
    "model_attributes_type": "Тело запроса должно быть объектом JSON",
    "missing_argument": "Обязательное поле",
}


def _field_path(loc: tuple) -> str:
    """`("body", "variables", 0, "name")` → `variables[0].name`."""
    parts = [p for p in loc if p not in ("body", "query", "path", "header", "cookie")]
    if not any(isinstance(p, str) for p in parts):
        return "body"  # ошибка разбора всего тела: в loc только позиция символа
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or "body"


def validation_fields(errors: list[dict]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for err in errors:
        fields.setdefault(_field_path(tuple(err.get("loc", ()))), _VALIDATION_MESSAGES.get(err.get("type", ""), "Некорректное значение"))
    return fields


_HTTP_MESSAGES = {
    404: ("not_found", "Не найдено"),
    405: ("method_not_allowed", "Этот метод для адреса не поддерживается"),
    413: ("payload_too_large", "Слишком большой запрос"),
}


def _ml_status(exc: MLError) -> int:
    if isinstance(exc, ExtractionError | RuleTemplateError):
        return 422
    if exc.retryable:
        return 503
    return 500 if isinstance(exc, AnalysisError) else 422


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DataError)
    async def data_error(_: Request, exc: DataError) -> JSONResponse:
        headers = None
        retry_after = exc.details.get("retry_after_seconds") if isinstance(exc.details, dict) else None
        if exc.status == 429 and retry_after:
            headers = {"Retry-After": str(retry_after)}
        return JSONResponse(status_code=exc.status, content=exc.to_dict(), headers=headers)

    @app.exception_handler(MLError)
    async def ml_error(_: Request, exc: MLError) -> JSONResponse:
        return error_response(_ml_status(exc), exc.code, exc.message, {"retryable": exc.retryable})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", "Проверьте заполнение полей", {"fields": validation_fields(exc.errors())})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_MESSAGES.get(exc.status_code, (f"http_{exc.status_code}", "Не удалось выполнить запрос"))
        return JSONResponse(status_code=exc.status_code, content=error_body(code, message), headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
        return error_response(500, "internal_error", "Ошибка на сервере. Повторите через минуту")
