from ml.analysis.finance import (
    financial_load,
    parse_deadline,
    parse_money,
    parse_percent,
    parse_term_days,
)


def fields(**values):
    return [{"key": k, "label": k, "value": v} for k, v in values.items()]


def test_parse_money_and_percent():
    assert parse_money("1 557 881,70 руб.") == 1557881.70
    assert parse_money("7789.41 РОССИЙСКИЙ РУБЛЬ") == 7789.41
    assert parse_money("1,5 млн руб.") == 1500000
    assert parse_money("500 тыс. рублей") == 500000
    assert parse_percent("10,00%") == 10
    assert parse_percent("5 процентов") == 5
    assert parse_percent("30 (тридцать) процентов") == 30
    assert parse_money("1 000 (одна тысяча) рублей") == 1000


def test_two_different_numbers_are_not_guessed():
    assert parse_money("от 1 000 руб. до 2 000 руб.") is None
    assert parse_percent("от 0,5% до 1%") is None


def test_term_days():
    assert parse_term_days("в течение 45 календарных дней с 11 января 2027 г.") == 45
    assert parse_term_days("не позднее 20 (двадцати) рабочих дней") == 28
    assert parse_term_days("в течение 7 рабочих дней с даты подписания") == 10
    assert parse_term_days("2 месяца") == 60
    assert parse_term_days("11 января 2027 г.") is None


def test_deadline():
    assert parse_deadline("05.11.2026 10:00") == "2026-11-05T10:00"
    assert parse_deadline("5 ноября 2026 г.") == "2026-11-05"
    assert parse_deadline("31.02.2026") is None
    assert parse_deadline("не указана") is None


def test_real_passport_shape():
    load = financial_load(fields(
        nmck="1 557 881,70 руб.", application_security="7 789,41 руб.", contract_security="10,00%",
        warranty_security="не требуется", advance="не предусмотрен",
        delivery_term="в течение 45 календарных дней с 11 января 2027 г.",
        payment_term="в течение 7 рабочих дней с даты подписания документа о приемке",
        deadline="05.11.2026 10:00"))
    by_key = {s.key: s for s in load.securities}
    assert by_key["application_security"].basis == "stated"
    assert by_key["contract_security"].amount == 155788.17
    assert by_key["contract_security"].basis == "percent_of_nmck"
    assert not by_key["warranty_security"].required
    assert load.total_security == 163577.58
    assert not load.total_incomplete
    assert load.advance_amount is None
    assert load.cash_cycle.total_days == 55
    assert load.deadline_at == "2026-11-05T10:00"


def test_stated_amount_wins_over_percent():
    item = financial_load(fields(nmck="1 000 000 руб.", contract_security="5% (60 000 руб.)")).securities[0]
    assert item.amount == 60000 and item.basis == "stated"


def test_percent_without_nmck_leaves_total_incomplete():
    load = financial_load(fields(contract_security="10%", application_security="5 000 руб."))
    assert load.total_security == 5000
    assert load.total_incomplete


def test_nothing_to_count():
    assert financial_load(fields(subject="Поставка мебели")) is None
