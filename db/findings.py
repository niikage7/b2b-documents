"""Находки: пересчёт цвета и счётчиков, ревью юриста, ручные пометки, история (FR-4.5, FR-6.1–6.4).

Формат ответов — как в «Требованиях к бэкенду»: `PATCH /api/findings/{id}` и
`POST /api/documents/{id}/findings` возвращают `{finding, document, batch}`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import derived
from db.errors import Forbidden, InvalidTransition, NotFound, ValidationFailed, VersionConflict
from db.models import Batch, BlockRow, Document, Finding, FindingEvent, Rule, User


def _id(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    """Дата в UTC в формате ISO 8601 с `Z`."""
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Пересчёт ------------------------------------------------------------------------------


def _counts(s: Session, *, batch_id: uuid.UUID) -> dict[uuid.UUID, dict]:
    """Активные находки по уровням и прогресс ревью для каждого документа пакета."""
    rows = s.execute(
        select(Finding.document_id, Finding.source, Finding.level, Finding.status, func.count())
        .where(Finding.batch_id == batch_id)
        .group_by(Finding.document_id, Finding.source, Finding.level, Finding.status)
    ).all()
    out: dict[uuid.UUID, dict] = defaultdict(lambda: {"critical": 0, "warning": 0, "checked": 0, "total": 0})
    for doc_id, source, level, status, n in rows:
        c = out[doc_id]
        if derived.is_active(status) and level in derived.LEVELS:
            c[level] += n
        if source == "ai":
            c["total"] += n
            if status in derived.DECIDED:
                c["checked"] += n
    return out


def batch_summary_counters(s: Session, batch_id) -> dict:
    """`worst_level`, `counters`, `review`, `findings` пакета — для `batch.done`, списка и шапки."""
    batch = s.get(Batch, _id(batch_id))
    if batch is None:
        raise NotFound("Пакет не найден")
    docs = s.execute(select(Document.id, Document.status, Document.worst_level)
                     .where(Document.batch_id == batch.id)).all()
    counts = _counts(s, batch_id=batch.id)
    counters = {"total": len(docs), "ok": 0, "warning": 0, "critical": 0, "errors": 0}
    for _, status, worst in docs:
        if status == "error":
            counters["errors"] += 1
        elif status == "done" and worst in ("ok", "warning", "critical"):
            counters[worst] += 1
    findings = {"critical": sum(c["critical"] for c in counts.values()),
                "warning": sum(c["warning"] for c in counts.values())}
    review = {"checked": sum(c["checked"] for c in counts.values()), "total": sum(c["total"] for c in counts.values())}
    worst = derived.batch_worst_level(batch.status, [w for _, st, w in docs if st == "done"])
    return {"worst_level": worst, "counters": counters, "review": review, "findings": findings}


def recalc(s: Session, document_id) -> dict:
    """Цвет и счётчики документа и пакета после анализа или любого изменения находки (FR-4.5)."""
    doc = s.get(Document, _id(document_id))
    if doc is None:
        raise NotFound("Документ не найден")
    counts = _counts(s, batch_id=doc.batch_id).get(doc.id, {"critical": 0, "warning": 0, "checked": 0, "total": 0})
    active = [lv for lv in derived.LEVELS if counts[lv]]
    doc.worst_level = derived.document_worst_level(doc.status, active)
    s.flush()
    return {
        "document": {
            "id": str(doc.id),
            "worst_level": doc.worst_level,
            "findings_summary": {"critical": counts["critical"], "warning": counts["warning"]},
            "review": {"checked": counts["checked"], "total": counts["total"]},
        },
        "batch": {"id": str(doc.batch_id), **batch_summary_counters(s, doc.batch_id)},
    }


# --- Находка для ответа API -------------------------------------------------------------------


def findings_payload(s: Session, findings: Sequence[Finding], *, viewer_id=None) -> list[dict]:
    """Находки в формате «Требований к бэкенду» с правилом, разделом, авторами и номером по порядку.

    `order` — номер среди активных находок документа по порядку блоков и `start`, с 1.
    """
    if not findings:
        return []
    blocks = {b.id: b for b in s.scalars(select(BlockRow).where(BlockRow.id.in_({f.block_id for f in findings})))}
    rule_ids = {f.rule_id for f in findings if f.rule_id}
    rules = {r.id: r for r in s.scalars(select(Rule).where(Rule.id.in_(rule_ids)))} if rule_ids else {}
    user_ids = {u for f in findings for u in (f.author_id, f.decided_by) if u}
    users = {u.id: u for u in s.scalars(select(User).where(User.id.in_(user_ids)))} if user_ids else {}

    order: dict[uuid.UUID, int] = {}
    doc_ids = {f.document_id for f in findings}
    active = s.execute(
        select(Finding.id, Finding.document_id).join(BlockRow, BlockRow.id == Finding.block_id)
        .where(Finding.document_id.in_(doc_ids), Finding.status != "rejected")
        .order_by(Finding.document_id, BlockRow.order, Finding.start, Finding.created_at)
    ).all()
    counter: dict[uuid.UUID, int] = defaultdict(int)
    for fid, doc_id in active:
        counter[doc_id] += 1
        order[fid] = counter[doc_id]

    def person(uid):
        user = users.get(uid)
        return {"id": str(user.id), "full_name": user.full_name} if user else None

    out = []
    for f in findings:
        block = blocks.get(f.block_id)
        rule = rules.get(f.rule_id) if f.rule_id else None
        out.append({
            "id": str(f.id),
            "document_id": str(f.document_id),
            "order": order.get(f.id),
            "source": f.source,
            "status": f.status,
            "level": f.level,
            "original_level": f.original_level,
            "rule": {"id": str(rule.id), "code": rule.code, "title": rule.title} if rule else None,
            "block_id": str(f.block_id),
            "start": f.start,
            "end": f.end,
            "quote": f.quote,
            "section_block_id": str(block.section_block_id) if block and block.section_block_id else None,
            "section_number": (block.section_number or block.number) if block else None,
            "section_title": block.section_title if block else None,
            "page": block.page if block else None,
            "explanation": f.explanation,
            "recommendation": f.recommendation,
            "confidence": f.confidence,
            "confidence_level": derived.confidence_level(f.confidence),
            "comment": f.comment,
            "author": person(f.author_id),
            "decided_by": person(f.decided_by),
            "decided_at": iso(f.decided_at),
            "can_delete": f.source == "user" and viewer_id is not None and f.author_id == _id(viewer_id),
            "version": f.version,
        })
    return out


def _finding(s: Session, finding_id, *, lock: bool = False) -> Finding:
    q = select(Finding).where(Finding.id == _id(finding_id))
    if lock:
        q = q.with_for_update()
    finding = s.scalar(q)
    if finding is None:
        raise NotFound("Находка не найдена")
    return finding


def _snapshot(f: Finding) -> dict:
    return {"status": f.status, "level": f.level, "explanation": f.explanation, "comment": f.comment}


# --- Ревью -----------------------------------------------------------------------------------


def update_finding(s: Session, finding_id, *, actor_id, version: int, status: str | None = None,
                   level: str | None = None, explanation: str | None = None, comment: str | None = None) -> dict:
    """`PATCH /api/findings/{id}`: решение юриста с проверкой версии и перехода статуса (FR-6.2, FR-6.4).

    Смена уровня или пояснения без явного статуса переводит находку в `edited`; возврат в `new`
    очищает решение и возвращает исходный уровень. Каждое изменение — событие в `finding_event`.
    """
    f = _finding(s, finding_id, lock=True)
    if f.version != version:
        raise VersionConflict(details={"current": findings_payload(s, [f], viewer_id=actor_id)[0]})
    if level is not None and level not in derived.LEVELS:
        raise ValidationFailed({"level": "Уровень — critical или warning"})
    old = _snapshot(f)
    edits = (level is not None and level != f.level) or (explanation is not None and explanation != f.explanation)
    target = status or ("edited" if edits else f.status)
    if target not in derived.FINDING_STATUSES:
        raise ValidationFailed({"status": "Неизвестный статус находки"})
    if not derived.can_transition(f.status, target):
        raise InvalidTransition()

    action = "commented"
    if target != f.status:
        action = {"accepted": "accepted", "rejected": "rejected", "edited": "edited", "new": "reopened"}[target]
    elif edits:
        action = "edited"
    if target == "new":
        f.decided_by = None
        f.decided_at = None
        if f.status == "edited":
            f.level = f.original_level
    elif target != f.status or edits:
        f.decided_by = _id(actor_id)
        f.decided_at = _now()
    f.status = target
    if level is not None and target != "new":
        f.level = level
    if explanation is not None:
        f.explanation = explanation
    if comment is not None:
        f.comment = comment or None
    f.version += 1
    s.add(FindingEvent(finding_id=f.id, actor_id=_id(actor_id), action=action, old=old, new=_snapshot(f)))
    s.flush()
    return {"finding": findings_payload(s, [f], viewer_id=actor_id)[0], **recalc(s, f.document_id)}


def create_user_finding(s: Session, document_id, *, actor_id, block_id, start: int, end: int, level: str,
                        comment: str | None = None) -> dict:
    """Ручная пометка (FR-6.1): сразу `accepted`, цитата вырезается из текста блока."""
    doc = s.get(Document, _id(document_id))
    if doc is None:
        raise NotFound("Документ не найден")
    block = s.get(BlockRow, _id(block_id))
    fields = {}
    if block is None or block.document_id != doc.id:
        fields["block_id"] = "Фрагмент не найден в этом документе"
    elif not (0 <= start < end <= len(block.text)):
        fields["start"] = "Выделение выходит за границы фрагмента"
    if level not in derived.LEVELS:
        fields["level"] = "Уровень — critical или warning"
    if fields:
        raise ValidationFailed(fields)
    f = Finding(batch_id=doc.batch_id, document_id=doc.id, source="user", level=level, original_level=level,
                status="accepted", block_id=block.id, start=start, end=end, quote=block.text[start:end],
                explanation=comment or "", comment=comment, author_id=_id(actor_id), decided_by=_id(actor_id),
                decided_at=_now())
    s.add(f)
    s.flush()
    s.add(FindingEvent(finding_id=f.id, actor_id=_id(actor_id), action="created", new=_snapshot(f)))
    s.flush()
    return {"finding": findings_payload(s, [f], viewer_id=actor_id)[0], **recalc(s, doc.id)}


def delete_user_finding(s: Session, finding_id, *, actor_id) -> dict:
    """`DELETE /api/findings/{id}`: только ручная пометка и только её автор."""
    f = _finding(s, finding_id, lock=True)
    if f.source != "user" or f.author_id != _id(actor_id):
        raise Forbidden("Удалить можно только свою ручную пометку")
    document_id = f.document_id
    s.delete(f)
    s.flush()
    return recalc(s, document_id)


def finding_history(s: Session, finding_id) -> dict:
    """`GET /api/findings/{id}/history` (FR-6.3)."""
    _finding(s, finding_id)
    rows = s.execute(
        select(FindingEvent, User.full_name).outerjoin(User, User.id == FindingEvent.actor_id)
        .where(FindingEvent.finding_id == _id(finding_id)).order_by(FindingEvent.created_at)
    ).all()
    return {"events": [
        {"action": e.action, "actor": {"full_name": name or "ИИ"}, "old": e.old, "new": e.new,
         "created_at": iso(e.created_at)}
        for e, name in rows
    ]}
