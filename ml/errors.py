"""Исключения ML-части с кодом и текстом для пользователя.

Влад превращает их в `document.error_code` и `document.error_message` (FR-1.2, FR-2.3):
`code` — машинный код, `message` — причина на русском, её показывают как есть.
"""

from __future__ import annotations


class MLError(Exception):
    code = "ml_error"
    default_message = "Не удалось обработать документ"
    retryable = False

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        self.message = message or self.default_message
        self.detail = detail
        super().__init__(self.message if detail is None else f"{self.message} ({detail})")


class ExtractionError(MLError):
    code = "extraction_failed"
    default_message = "Не удалось извлечь текст из документа"


class UnsupportedFormatError(ExtractionError):
    code = "unsupported"
    default_message = "Формат не поддерживается — файл не будет проверен"


class EncryptedFileError(ExtractionError):
    code = "encrypted"
    default_message = "Файл защищён паролем — проверить его не получится"


class CorruptedFileError(ExtractionError):
    code = "corrupted"
    default_message = "Файл повреждён и не открывается"


class TooManyPagesError(ExtractionError):
    code = "too_many_pages"
    default_message = "В документе больше 500 страниц — такой объём не проверяется"


class ConversionError(ExtractionError):
    code = "conversion_failed"
    default_message = "Не удалось преобразовать файл DOC или RTF для проверки"
    retryable = True


class OcrError(ExtractionError):
    code = "ocr_failed"
    default_message = "Не удалось распознать текст скана"
    retryable = True


class OcrSetupError(OcrError):
    """Окружение OCR неполное (нет модели языка): повтор этапа не поможет."""

    default_message = "Распознавание сканов недоступно: на сервере не установлена модель языка — обратитесь к администратору"
    retryable = False


class AnalysisError(MLError):
    code = "analysis_failed"
    default_message = "Не удалось проверить документ по правилам"


class LLMError(AnalysisError):
    code = "llm_unavailable"
    default_message = "Сервис проверки ИИ временно недоступен"
    retryable = True


class LLMResponseError(AnalysisError):
    code = "llm_bad_response"
    default_message = "Сервис проверки ИИ вернул ответ в неверном формате"
    retryable = True


class RuleTemplateError(AnalysisError):
    code = "rule_template"
    default_message = "В инструкции правила есть ошибка"


class TextTooLongError(AnalysisError):
    code = "text_too_long"
    default_message = "Текст для проверки длиннее 20 000 символов"


class RuleTestTimeoutError(AnalysisError):
    code = "test_timeout"
    default_message = "Проверка правила не уложилась в 60 секунд — попробуйте текст короче"
    retryable = True
