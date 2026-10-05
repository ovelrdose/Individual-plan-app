"""Тест-сценарии движка TZ.md §7.3 для групп ЛФК, бассейна и тренажёров (без БД)."""

from datetime import date, timedelta

from apps.scheduling.domain import (
    Busy,
    EquipmentLoad,
    IssueCode,
    Need,
    Session,
    Snapshot,
    propose,
)
from apps.scheduling.domain.model import Kind

COURSE = tuple(date(2026, 10, 5) + timedelta(days=i) for i in range(15))  # ШРМ 4
ST_STARTS = tuple(13 * 60 + 15 * k for k in range(8))  # 13:00 … 14:45


def m(hours: int, minutes: int = 0) -> int:
    return hours * 60 + minutes


def group(
    pid: int,
    label: str,
    *starts: int,
    kind: Kind = Kind.LFK_GROUP,
    dates=COURSE,
    per_day: int = 1,
    pinned_units=(),
) -> Need:
    sessions = tuple(Session(pid * 10 + i, s, s + 30) for i, s in enumerate(starts))
    return Need(pid, pid, kind, label, dates, sessions=sessions, per_day=per_day,
                pinned_units=pinned_units)  # fmt: skip


def equipment(pid: int, *, capacity: int = 1, per_day: int = 1, dates=COURSE) -> Need:
    return Need(pid, pid, Kind.EQUIPMENT, "st-150", dates, equipment_id=1, starts=ST_STARTS,
                duration=15, capacity=capacity, per_day=per_day)  # fmt: skip


def times(proposal, pid: int) -> set[int]:
    return {p.start for p in proposal.placements if p.prescription_id == pid}


def test_scenario_1_everything_placed_same_time_every_day():
    snapshot = Snapshot(
        needs=(group(1, "Эрго общая", m(9, 10)), group(2, "I can нога", m(10, 30)), equipment(3))
    )

    proposal = propose(snapshot)

    assert proposal.issues == []
    for pid in (1, 2, 3):
        days = [p.date for p in proposal.placements if p.prescription_id == pid]
        assert days == list(COURSE)
    assert times(proposal, 1) == {m(9, 10)}
    assert times(proposal, 2) == {m(10, 30)}
    assert times(proposal, 3) == {m(13)}


def test_scenario_2_second_group_moves_to_its_other_session():
    snapshot = Snapshot(
        needs=(group(1, "I can нога", m(10, 30)), group(2, "Нейро-тренинг", m(10, 30), m(14, 20)))
    )

    proposal = propose(snapshot)

    assert times(proposal, 1) == {m(10, 30)}
    assert times(proposal, 2) == {m(14, 20)}
    assert proposal.issues == []


def test_overlap_without_alternative_is_a_conflict_not_a_double_booking():
    snapshot = Snapshot(needs=(group(1, "Эрго общая", m(9, 10)), group(2, "Другая", m(9, 20))))

    proposal = propose(snapshot)

    assert times(proposal, 2) == set()
    (conflict,) = proposal.conflicts
    assert conflict.code == IssueCode.GROUP_OVERLAP
    assert conflict.message.startswith("Другая: 05.10, 06.10")


def test_scenario_5_pool_interval_blocks_overlapping_group():
    # Бассейн 09:45–10:15 пересекается с группой 09:50–10:20 — реальные интервалы.
    snapshot = Snapshot(
        needs=(
            group(1, "Вестибулярная гимнастика", m(9, 50), m(15, 40)),
            group(2, "ЛФК в воде: нижняя", m(9, 45), kind=Kind.POOL),
        )
    )

    proposal = propose(snapshot)

    assert times(proposal, 1) == {m(9, 50)}
    assert times(proposal, 2) == set(), "группы ЛФК ставятся раньше бассейна"
    assert proposal.conflicts[0].code == IssueCode.GROUP_OVERLAP


def test_pool_without_group_is_a_conflict():
    proposal = propose(
        Snapshot(needs=(Need(1, 1, Kind.POOL, "Бассейн", COURSE, group_required=True),))
    )

    assert [c.code for c in proposal.conflicts] == [IssueCode.POOL_TYPE_REQUIRED]
    assert proposal.placements == []


def test_group_without_sessions_is_a_conflict():
    proposal = propose(Snapshot(needs=(Need(1, 1, Kind.LFK_GROUP, "Лого-тренинг", COURSE),)))

    assert [c.code for c in proposal.conflicts] == [IssueCode.NO_SESSIONS]


def test_scenario_7_equipment_capacity():
    load = tuple(EquipmentLoad(1, day, m(13), m(13, 15)) for day in COURSE for _ in range(2))

    proposal = propose(Snapshot(needs=(equipment(1, capacity=2),), equipment_load=load))

    assert times(proposal, 1) == {m(13, 15)}


def test_equipment_spreads_load_between_times():
    # 13:00 уже занят одним из двух мест, 13:15 свободен — пациент идёт на 13:15.
    load = tuple(EquipmentLoad(1, day, m(13), m(13, 15)) for day in COURSE)

    proposal = propose(Snapshot(needs=(equipment(1, capacity=2),), equipment_load=load))

    assert times(proposal, 1) == {m(13, 15)}


def test_equipment_twice_a_day_gets_two_different_times():
    proposal = propose(Snapshot(needs=(equipment(1, per_day=2),)))

    assert times(proposal, 1) == {m(13), m(13, 15)}
    assert len(proposal.placements) == 30


def test_time_free_on_all_days_beats_a_deviation():
    load = (EquipmentLoad(1, COURSE[3], m(13), m(13, 15)),)

    proposal = propose(Snapshot(needs=(equipment(1),), equipment_load=load))

    assert times(proposal, 1) == {m(13, 15)}
    assert proposal.issues == []


def test_day_deviation_when_no_time_is_free_on_all_days():
    # 08.10 свободно только 13:15, 10.10 занято только 13:15 — общего времени нет.
    load = tuple(EquipmentLoad(1, COURSE[3], s, s + 15) for s in ST_STARTS if s != m(13, 15))
    load += (EquipmentLoad(1, COURSE[5], m(13, 15), m(13, 30)),)

    proposal = propose(Snapshot(needs=(equipment(1),), equipment_load=load))

    assert {p.start for p in proposal.placements if p.date != COURSE[3]} == {m(13)}
    assert [p.start for p in proposal.placements if p.date == COURSE[3]] == [m(13, 15)]
    (warning,) = proposal.warnings
    assert warning.code == IssueCode.DAY_DEVIATION
    assert "08.10 — в 13:15 вместо 13:00" in warning.message
    assert proposal.conflicts == []


def test_equipment_unplaced_when_window_is_full():
    full_day = COURSE[0]
    load = tuple(EquipmentLoad(1, full_day, s, s + 15) for s in ST_STARTS)

    proposal = propose(Snapshot(needs=(equipment(1),), equipment_load=load))

    assert full_day not in {p.date for p in proposal.placements}
    assert proposal.conflicts[0].code == IssueCode.EQUIPMENT_UNPLACED
    assert "05.10" in proposal.conflicts[0].message


def test_scenario_8_pinned_busy_is_respected():
    busy = tuple(Busy(day, m(9, 0), m(9, 30)) for day in COURSE)

    proposal = propose(
        Snapshot(needs=(group(1, "Эрго общая", m(9, 10), m(15, 40)),), patient_busy=busy)
    )

    assert times(proposal, 1) == {m(15, 40)}


def test_prescription_dates_are_respected():
    later = COURSE[5:]

    proposal = propose(Snapshot(needs=(group(1, "Эрго общая", m(9, 10), dates=later),)))

    assert [p.date for p in proposal.placements] == list(later)


def test_deterministic():
    snapshot = Snapshot(
        needs=(
            group(1, "Эрго общая", m(9, 10)),
            group(2, "Нейро-тренинг", m(10, 30), m(14, 20)),
            equipment(3, per_day=2),
        ),
        equipment_load=(EquipmentLoad(1, COURSE[2], m(13), m(13, 15)),),
    )

    assert propose(snapshot) == propose(snapshot)


def test_individual_without_instructors_is_a_conflict():
    # Индивидуальные — шаг 3 (test_engine_individual.py): без инструкторов не поставить.
    proposal = propose(Snapshot(needs=(Need(1, 1, Kind.INDIVIDUAL, "Инд.занятие", COURSE),)))

    assert proposal.placements == []
    assert [issue.code for issue in proposal.issues] == [IssueCode.INDIVIDUAL_UNPLACED]


def test_group_order_does_not_matter():
    # Сценарий 2 в обратном порядке: у «I can нога» одно занятие, она ставится первой.
    snapshot = Snapshot(
        needs=(group(1, "Нейро-тренинг", m(10, 30), m(14, 20)), group(2, "I can нога", m(10, 30)))
    )

    proposal = propose(snapshot)

    assert times(proposal, 1) == {m(14, 20)}
    assert times(proposal, 2) == {m(10, 30)}
    assert proposal.issues == []


def test_pool_group_without_sessions_is_not_asked_to_choose_group():
    proposal = propose(Snapshot(needs=(Need(1, 1, Kind.POOL, "ЛФК в воде: спина", COURSE),)))

    assert [c.code for c in proposal.conflicts] == [IssueCode.NO_SESSIONS]


def test_inactive_and_impossible_equipment_are_conflicts():
    off = Need(1, 1, Kind.EQUIPMENT, "st-150", COURSE, equipment_id=1, starts=ST_STARTS,
               duration=15, unavailable="тренажёр «st-150» выключен в справочнике")  # fmt: skip
    empty = Need(2, 2, Kind.EQUIPMENT, "Pablo", COURSE, equipment_id=2, starts=(), duration=15)

    proposal = propose(Snapshot(needs=(off, empty)))

    assert proposal.placements == []
    assert [c.code for c in proposal.conflicts] == [IssueCode.EQUIPMENT_UNPLACED] * 2
    assert "выключен" in proposal.conflicts[0].message
    assert "не помещается в окно" in proposal.conflicts[1].message


def test_repeated_conflict_is_reported_once():
    two_starts = Need(1, 1, Kind.EQUIPMENT, "st-150", COURSE[:1], equipment_id=1,
                      starts=(m(13), m(13, 15)), duration=15, per_day=4)  # fmt: skip

    proposal = propose(Snapshot(needs=(two_starts,)))

    assert len(proposal.placements) == 2
    assert len(proposal.conflicts) == 1


def test_pinned_unit_counts_towards_per_day():
    day = COURSE[0]
    need = Need(1, 1, Kind.EQUIPMENT, "st-150", (day,), equipment_id=1, starts=ST_STARTS,
                duration=15, per_day=2, pinned_units=((day, 1),))  # fmt: skip

    proposal = propose(Snapshot(needs=(need,), patient_busy=(Busy(day, m(14), m(14, 15)),)))

    assert len(proposal.placements) == 1


# --- «N р/д» у групп: каждая группа N раз в день (решение владельца 04.10) -------------------


def test_group_twice_a_day_takes_two_sessions():
    snapshot = Snapshot(needs=(group(1, "Нейро-тренинг", m(10, 30), m(14, 20), per_day=2),))

    proposal = propose(snapshot)

    assert proposal.issues == []
    for day in COURSE:
        starts = sorted(p.start for p in proposal.placements if p.date == day)
        assert starts == [m(10, 30), m(14, 20)]


def test_group_twice_a_day_with_one_session_is_a_conflict():
    snapshot = Snapshot(
        needs=(
            group(1, "Эрго общая", m(9, 10), per_day=2),
            group(2, "I can нога", m(10, 30), per_day=2),
        )
    )

    proposal = propose(snapshot)

    assert times(proposal, 1) == {m(9, 10)}
    assert len([p for p in proposal.placements if p.prescription_id == 1]) == len(COURSE)
    assert [(i.code, i.message) for i in proposal.conflicts] == [
        (
            IssueCode.GROUP_FREQUENCY,
            "Эрго общая: назначено 2 р/д, а в расписании групп 1 занятие в день — поставлено 1.",
        ),
        (
            IssueCode.GROUP_FREQUENCY,
            "I can нога: назначено 2 р/д, а в расписании групп 1 занятие в день — поставлено 1.",
        ),
    ]


def test_second_unit_of_group_is_blocked_by_other_group():
    # I can нога занимает 10:30 — второе занятие Нейро-тренинга ставить некуда.
    snapshot = Snapshot(
        needs=(
            group(1, "I can нога", m(10, 30)),
            group(2, "Нейро-тренинг", m(10, 30), m(14, 20), per_day=2),
        )
    )

    proposal = propose(snapshot)

    assert times(proposal, 2) == {m(14, 20)}
    (conflict,) = proposal.conflicts
    assert conflict.code == IssueCode.GROUP_OVERLAP


def test_pinned_group_unit_counts_towards_per_day():
    day = COURSE[0]
    snapshot = Snapshot(
        needs=(
            group(1, "Нейро-тренинг", m(10, 30), m(14, 20), per_day=2, pinned_units=((day, 1),)),
        ),
        patient_busy=(Busy(day, m(10, 30), m(11)),),
    )

    proposal = propose(snapshot)

    assert sorted(p.start for p in proposal.placements if p.date == day) == [m(14, 20)]
    assert proposal.issues == []
