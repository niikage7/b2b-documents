from __future__ import annotations

from db import derived


def test_worst_level_and_status():
    assert derived.document_worst_level("error", ["critical"]) == "not_checked"
    assert derived.document_worst_level("processing", []) is None
    assert derived.document_worst_level("done", []) == "ok"
    assert derived.document_worst_level("done", ["warning", "critical"]) == "critical"
    assert derived.batch_worst_level("processing", ["critical"]) is None
    assert derived.batch_worst_level("partial_error", ["ok", "warning"]) == "warning"
    assert derived.batch_worst_level("partial_error", []) == "not_checked"
    assert derived.batch_status(["done", "done"]) == "done"
    assert derived.batch_status(["done", "error"]) == "partial_error"
    assert derived.batch_status(["done", "processing"]) == "processing"


def test_transitions_and_confidence():
    assert derived.can_transition("new", "rejected") and derived.can_transition("edited", "accepted")
    assert not derived.can_transition("accepted", "rejected")
    assert derived.can_transition("accepted", "new")
    assert [derived.confidence_level(c) for c in (0.85, 0.5, 0.2, None)] == ["high", "medium", "low", None]
    assert derived.initials("Мария Волкова") == "МВ"
