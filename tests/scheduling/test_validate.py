"""Единый валидатор (TZ.md §7.2, FR-SCH-5): каждое нарушение и его вид — без БД."""

from datetime import date

import pytest

from apps.scheduling.domain.validate import (
    Candidate,
    Context,
    Severity,
    Taken,
    ViolationCode,
    blocking,
    validate,
)

DAY = date(2026, 10, 5)


def m(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


def individual(start: int = m(9, 50), end: int | None = None) -> Candidate:
    return Candidate(DAY, start, end or start + 30, individual=True)


def codes(candidate: Candidate, context: Context) -> list[ViolationCode]:
    return [v.code for v in validate(candidate, context)]


def test_clean_individual_has_no_violations():
    assert validate(individual(), Context()) == []


def test_patient_overlap_compares_real_intervals():
    """Бассейн 09:45–10:15 пересекается со слотом 09:50–10:20 (§7.2)."""
    pool = Taken(m(9, 45), m(10, 15), "Бассейн нижняя конечность")

    result = validate(individual(), Context(patient=(pool,)))

    assert [v.code for v in result] == [ViolationCode.PATIENT_OVERLAP]
    assert result[0].message == (
        "У пациента в это время уже Бассейн нижняя конечность (9:45–10:15)."
    )
    assert result[0].severity is Severity.BLOCKING


def test_touching_intervals_do_not_overlap():
    group = Taken(m(9, 10), m(9, 50), "Эрго общая")

    assert codes(individual(), Context(patient=(group,))) == []


def test_instructor_off_shift_needs_confirmation():
    result = validate(individual(), Context(instructor_working=False))

    assert [v.code for v in result] == [ViolationCode.INSTRUCTOR_OFF_SHIFT]
    assert result[0].severity is Severity.CONFIRM


def test_instructor_busy_by_duty_and_by_patient():
    result = validate(
        individual(), Context(instructor_busy="Метод. работа", instructor_patient="3п Петров")
    )

    assert [v.code for v in result] == [ViolationCode.INSTRUCTOR_BUSY] * 2
    assert result[0].message == "У инструктора в этом слоте Метод. работа."
    assert result[1].message == "У инструктора в этом слоте уже пациент 3п Петров."
    assert all(v.severity is Severity.BLOCKING for v in result)


def test_individual_limit_counts_existing_individuals():
    other = Taken(m(13), m(13, 30), "Инд.занятие", individual=True)

    result = validate(individual(), Context(patient=(other,), individual_limit=1))

    assert [v.code for v in result] == [ViolationCode.INDIVIDUAL_LIMIT]
    assert result[0].message == "Индивидуальных в этот день станет 2, а можно 1."
    assert result[0].severity is Severity.CONFIRM


@pytest.mark.parametrize(
    ("other_start", "adjacent"),
    [
        (m(9, 10), True),  # 9:10–9:40 и 9:50 — перерыв 10 минут
        (m(10, 30), True),  # после 10:20 — перерыв 10 минут
        (m(11, 10), False),  # перерыв 50 минут
    ],
)
def test_individual_adjacent(other_start, adjacent):
    other = Taken(other_start, other_start + 30, "Инд.занятие", individual=True)

    result = codes(individual(), Context(patient=(other,)))

    assert (ViolationCode.INDIVIDUAL_ADJACENT in result) is adjacent


def test_lunch_break_slots_are_not_adjacent():
    """11:10–11:40 и 13:00–13:30 соседними не считаются (§7.2)."""
    before = Taken(m(11, 10), m(11, 40), "Инд.занятие", individual=True)

    assert codes(individual(m(13)), Context(patient=(before,))) == []


def test_adjacency_ignores_non_individual():
    group = Taken(m(9, 10), m(9, 40), "Эрго общая")

    assert codes(individual(), Context(patient=(group,))) == []


def test_out_of_course():
    result = validate(individual(), Context(in_course=False))

    assert [v.code for v in result] == [ViolationCode.OUT_OF_COURSE]
    assert result[0].message == "05.10 — вне курса или вне дат назначения."
    assert result[0].severity is Severity.CONFIRM


def test_equipment_window_and_full():
    candidate = Candidate(DAY, m(15, 10), m(15, 25), equipment=True)

    result = validate(
        candidate,
        Context(equipment_starts=(m(13), m(13, 15)), equipment_taken=2, equipment_capacity=2),
    )

    assert [v.code for v in result] == [
        ViolationCode.EQUIPMENT_WINDOW,
        ViolationCode.EQUIPMENT_FULL,
    ]
    assert result[0].severity is Severity.CONFIRM
    assert result[1].severity is Severity.NOTE
    assert result[1].message == "На тренажёре в это время уже 2 при вместимости 2."


def test_equipment_in_window_with_room_is_clean():
    candidate = Candidate(DAY, m(13), m(13, 15), equipment=True)

    assert validate(candidate, Context(equipment_starts=(m(13),), equipment_capacity=2)) == []


def test_individual_checks_do_not_apply_to_groups():
    group = Candidate(DAY, m(9, 10), m(9, 40))

    assert validate(group, Context(instructor_working=False, instructor_busy="БОС")) == []


def test_blocking_filters_only_unfixable():
    result = validate(
        individual(),
        Context(instructor_busy="БОС", individual_limit=0, in_course=False),
    )

    assert [v.code for v in blocking(result)] == [ViolationCode.INSTRUCTOR_BUSY]
