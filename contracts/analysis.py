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
- У каждого ответа есть вердикт `risk`: модель может вернуть условие, которое проверила и
  в котором риска не нашла (`risk=false`). Такие ответы в находки не идут, движок пишет их
  в лог с причиной `no_risk`. Без вердикта модель отмечала условия в пределах порога: на
  открытых пакетах 46 ложных находок из 51 (docs/ml-plan.md).
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
    check: str = Field("", description="Что сказано в условии и с каким порогом правила оно сравнивается")
    risk: bool = Field(True, description="Вердикт: условие нарушает правило. false — проверено, риска нет")
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


class LLMVerdict(BaseModel):
    """Второй взгляд на кандидата: одно правило, одна цитата с соседним текстом — только вердикт."""

    check: str = Field("", description="Что сказано в условии и с каким порогом правила оно сравнивается")
    risk: bool = Field(description="Условие нарушает именно это правило")


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
    reason: Literal["quote_not_found", "unknown_rule", "duplicate", "empty_quote", "no_risk", "not_confirmed"]
    best_score: float | None = None
    note: str | None = Field(None, description="Как модель сравнила условие с правилом (`check`)")


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


class PassportField(BaseModel):
    """Поле паспорта закупки: значение и место в документе, откуда оно взято."""

    key: str = Field(description="nmck, contract_security, delivery_term, … — список в ml.analysis.passport")
    label: str = Field(description="Подпись для карточки: «НМЦК», «Срок поставки»")
    value: str = Field(description="Короткое значение для карточки")
    quote: str = Field(description="Ровно `block.text[start:end]`")
    document_id: str
    filename: str | None = None
    block_id: str
    start: int
    end: int
    page: int | None = None
    section_number: str | None = None


class ProcurementPassport(BaseModel):
    """Паспорт закупки (`batch.meta`): ключевые условия пакета одной карточкой."""

    fields: list[PassportField]
    usage: LLMUsage = Field(default_factory=LLMUsage)

    def value(self, key: str) -> str | None:
        return next((f.value for f in self.fields if f.key == key), None)


class FindingRef(BaseModel):
    """Другое место в пакете, с которым связана находка: второй документ противоречия (FR-4.4*)."""

    document_id: str
    filename: str | None = None
    block_id: str
    start: int
    end: int
    quote: str = Field(description="Ровно `block.text[start:end]`")
    value: str | None = Field(None, description="Значение условия в этом месте: «45 календарных дней»")
    page: int | None = None
    section_number: str | None = None


class PackageFinding(BaseModel):
    """Находка проверки пакета: не правило чек-листа, а встроенная проверка (`code`: A-01, X-01).

    `related` — другие места того же условия: находка противоречия лежит в каждом из документов и ссылается
    на остальные.
    """

    code: str
    title: str
    level: Level
    document_id: str
    filename: str | None = None
    block_id: str
    start: int
    end: int
    quote: str = Field(description="Ровно `block.text[start:end]`")
    explanation: str
    recommendation: str | None = None
    confidence: float = Field(ge=0, le=1)
    page: int | None = None
    section_number: str | None = None
    related: list[FindingRef] = Field(default_factory=list)


class PackageCheckResult(BaseModel):
    """Результат `check_package`: находки без LLM и межфайловые, учёт токенов."""

    findings: list[PackageFinding] = Field(default_factory=list)
    usage: LLMUsage = Field(default_factory=LLMUsage)


class PackageAnswer(BaseModel):
    """Ответ на вопрос по документам пакета: `citations` привязаны к блокам, иначе `found=false`."""

    found: bool
    answer: str
    citations: list[FindingRef] = Field(default_factory=list)
    usage: LLMUsage = Field(default_factory=LLMUsage)


class BriefRisk(BaseModel):
    finding_id: str | None = None
    document_id: str | None = None
    title: str
    level: Level
    document: str | None = None
    section_number: str | None = None
    page: int | None = None
    quote: str = ""


class PackageBrief(BaseModel):
    """Вывод по пакету для руководителя. `decision` считает код по находкам, текст пишет модель."""

    decision: Literal["go", "caution", "clarify"]
    decision_label: str
    summary: str
    top_risks: list[BriefRisk] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    not_checked: int = Field(0, description="Документов не проверено из-за ошибки: вывод по ним неполный")
    usage: LLMUsage = Field(default_factory=LLMUsage)


class RuleDraftResult(BaseModel):
    """Черновик правила из описания риска; прошёл `validate_rule`, ничего не сохранено."""

    title: str
    description: str
    level: Level
    kind: RuleKind
    prompt_template: str
    variables: list[RuleVariable] = Field(default_factory=list)
    risk_example: str = ""
    safe_example: str = ""
    warnings: list[str] = Field(default_factory=list)
    usage: LLMUsage = Field(default_factory=LLMUsage)


class RuleTestDropped(BaseModel):
    """Что модель рассмотрела, но в находки не попало: методолог видит, почему правило промолчало."""

    quote: str
    reason: str = Field(description="Причина на русском")
    note: str | None = Field(None, description="Как модель сравнила условие с правилом")


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
    considered: list[RuleTestDropped] = Field(default_factory=list)
