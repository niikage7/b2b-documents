"""Контракт 6: ответ LLM до привязки цитат и кандидаты в находки после привязки.

Владелец — Артём. Используют: Андрей (`merge_findings` пишет кандидатов в `finding`),
Влад (`POST /api/rules/test`).

Здесь же — правило в том виде, в каком его получает движок (`RuleSpec`). Схему правила
с переменными ведут Андрей и Артём вместе; поля совпадают с «Правилом целиком» из
документа «Требования к бэкенду».

Решения, которые стоит подтвердить с командой:

- Уровень находки берётся из правила, а не из ответа LLM: модель не выбирает
  `critical` или `warning`.
- Корень ответа LLM — объект `{"rules": [...]}`, а не массив: режим структурированного
  JSON у провайдеров принимает только объект.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Level = Literal["critical", "warning"]
RuleKind = Literal["semantic_llm", "llm"]
VariableType = Literal["text", "number", "list"]

SYSTEM_VARIABLES: dict[str, str] = {
    "фрагмент": "Найденный фрагмент документа",
    "предмет_закупки": "Из извещения",
}
"""Системные переменные шаблона: их заполняет система, в `rule.variables` они не хранятся."""


class RuleVariable(BaseModel):
    name: str
    type: VariableType
    value: str


class RuleSpec(BaseModel):
    """Правило, как его исполняет движок. Версия — та, что зафиксирована на старте пакета."""

    id: str | None = None
    version_id: str | None = None
    code: str
    title: str
    description: str
    level: Level
    kind: RuleKind
    prompt_template: str
    variables: list[RuleVariable] = Field(default_factory=list)
    enabled: bool = True
    query_embedding: list[float] | None = Field(
        None, description="`rule_version.query_embedding`: эмбеддинг строки поиска для semantic_llm"
    )


# --- Ответ LLM до привязки цитат --------------------------------------------------------


class LLMFinding(BaseModel):
    fragment: int | None = Field(None, description="Номер фрагмента из запроса, где стоит цитата")
    quote: str = Field(description="Дословная цитата из фрагмента")
    explanation: str = Field(description="Почему это риск для поставщика, 1–3 предложения")
    recommendation: str = Field("", description="Что поставщику сделать с этим риском, одно предложение")
    confidence: float = Field(description="Уверенность 0–1; значения вне диапазона обрезаются")

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if number > 1 and number <= 100:
            number /= 100  # модель ответила в процентах
        return min(1.0, max(0.0, number))


class LLMRuleAnswer(BaseModel):
    rule_code: str
    findings: list[LLMFinding] = Field(default_factory=list, description="Пустой список — «нет риска»")


class LLMGroupResponse(BaseModel):
    rules: list[LLMRuleAnswer]


# --- После привязки цитат -----------------------------------------------------------------


class FindingCandidate(BaseModel):
    """Кандидат в находку: цитата привязана к блоку, смещения точные.

    Раздел и страницу `merge_findings` берёт из блока `block_id`, а не из ответа LLM.
    """

    rule_code: str
    rule_id: str | None = None
    rule_version_id: str | None = None
    level: Level
    block_id: str
    start: int
    end: int
    quote: str = Field(description="Ровно `block.text[start:end]`")
    explanation: str
    recommendation: str | None = Field(None, description="Что поставщику сделать: запросить разъяснение, "
                                                          "предложить изменить условие, заложить риск в цену")
    confidence: float = Field(ge=0, le=1)
    match_score: float = Field(description="Совпадение цитаты LLM с текстом блока, 0–100")


class DroppedFinding(BaseModel):
    """Ответ LLM, который не попал в находки. Пишется в лог с причиной."""

    rule_code: str
    quote: str
    reason: Literal["quote_not_found", "unknown_rule", "duplicate", "empty_quote"]
    best_score: float | None = None


class LLMUsage(BaseModel):
    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = Field(0, description="Из них рассуждения модели: входят в completion_tokens")
    cost: float = Field(0.0, description="Стоимость в долларах, если провайдер её сообщает")

    def add(self, other: LLMUsage) -> None:
        self.calls += other.calls
        self.cached_calls += other.cached_calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost += other.cost


class RuleGroupResult(BaseModel):
    """Результат `analyze_rule_group`: вход `merge_findings`."""

    candidates: list[FindingCandidate]
    dropped: list[DroppedFinding] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list, description="Коды правил, которые прошли проверку")
    usage: LLMUsage = Field(default_factory=LLMUsage)


class RuleTestFinding(BaseModel):
    quote: str
    block_id: str
    start: int
    end: int
    section_number: str | None = None
    page: int | None = None
    level: Level
    explanation: str
    recommendation: str | None = None
    confidence: float


class RuleTestResult(BaseModel):
    """Ответ `test_rule` (FR-9.3). Ничего не пишется в БД."""

    findings: list[RuleTestFinding]
    duration_ms: int
    dropped: int = Field(0, description="Сколько ответов LLM отброшено: цитата не нашлась в тексте")
