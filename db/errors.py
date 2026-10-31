"""Ошибки слоя данных: код, текст на русском для пользователя и HTTP-статус для API.

API превращает их в ответ `{"error": {"code", "message", "details"}}` без разбора причин.
"""

from __future__ import annotations


class DataError(Exception):
    status = 400
    code = "bad_request"
    message = "Некорректный запрос"

    def __init__(self, message: str | None = None, *, code: str | None = None, details: dict | None = None):
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFound(DataError):
    status = 404
    code = "not_found"
    message = "Объект не найден"


class Forbidden(DataError):
    status = 403
    code = "forbidden"
    message = "Недостаточно прав"


class Conflict(DataError):
    status = 409
    code = "conflict"
    message = "Действие недоступно в текущем состоянии"


class VersionConflict(Conflict):
    code = "version_conflict"
    message = "Объект уже изменил другой пользователь. Обновите страницу"


class InvalidTransition(Conflict):
    code = "invalid_transition"
    message = "Такое изменение статуса недоступно"


class ValidationFailed(DataError):
    status = 422
    code = "validation_error"
    message = "Проверьте заполнение полей"

    def __init__(self, fields: dict[str, str], message: str | None = None, *, code: str | None = None,
                 warnings: list[str] | None = None):
        details: dict = {"fields": fields}
        if warnings:
            details["warnings"] = warnings
        super().__init__(message or next(iter(fields.values()), None), code=code, details=details)


class AuthError(DataError):
    status = 401
    code = "invalid_credentials"
    message = "Неверный логин или пароль"
