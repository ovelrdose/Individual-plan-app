"""Подбор групп, бассейна и тренажёров на курс (TZ.md FR-SCH-2): чистый Python, без базы.

Время — минуты от полуночи: 540 = 9:00.
"""

from datetime import date

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

DAYS = (date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8))


def group(pk: int = 1, sessions=((1, 540, 570),), **fields) -> Need:
    return Need(
        prescription_id=pk,
        procedure_id=pk,
        kind=Kind.LFK_GROUP,
        label=f"Группа {pk}",
        dates=DAYS,
        sessions=tuple(Session(*s) for s in sessions),
        **fields,
    )


def equipment(pk: int = 10, starts=(780, 795, 810), **fields) -> Need:
    values = {
        "prescription_id": pk,
        "procedure_id": pk,
        "kind": Kind.EQUIPMENT,
        "label": "st-150",
        "dates": DAYS,
        "equipment_id": 1,
        "starts": starts,
        "duration": 15,
        "capacity": 1,
    } | fields
    return Need(**values)


def times(proposal, pk: int) -> list[tuple[date, int]]:
    return sorted((p.date, p.start) for p in proposal.placements if p.prescription_id == pk)


def codes(proposal) -> list[IssueCode]:
    return [issue.code for issue in proposal.issues]


class TestGroups:
    def test_one_time_for_whole_course(self):
        proposal = propose(Snapshot(needs=(group(sessions=((1, 540, 570), (2, 600, 630))),)))
        assert times(proposal, 1) == [(day, 540) for day in DAYS]
        assert proposal.issues == []

    def test_busy_day_gets_replacement_with_warning(self):
        # 9:00 и 10:00 подходят в два дня из трёх: при равном покрытии — раннее время,
        # а в день, где 9:00 занято, — замена на 10:00 с предупреждением.
        busy = (Busy(DAYS[1], 540, 570), Busy(DAYS[2], 600, 630))
        need = group(sessions=((1, 540, 570), (2, 600, 630)))
        proposal = propose(Snapshot(needs=(need,), patient_busy=busy))

        assert times(proposal, 1) == [(DAYS[0], 540), (DAYS[1], 600), (DAYS[2], 540)]
        assert codes(proposal) == [IssueCode.DAY_DEVIATION]
        assert "07.10 — в 10:00 вместо 9:00" in proposal.warnings[0].message

    def test_no_time_left_is_conflict(self):
        busy = tuple(Busy(day, 500, 700) for day in DAYS[:1])
        proposal = propose(Snapshot(needs=(group(),), patient_busy=busy))

        assert times(proposal, 1) == [(DAYS[1], 540), (DAYS[2], 540)]
        assert codes(proposal) == [IssueCode.GROUP_OVERLAP]
        assert proposal.conflicts[0].is_conflict

    def test_two_groups_do_not_overlap(self):
        first = group(1, sessions=((1, 540, 570),))
        second = group(2, sessions=((2, 540, 570), (3, 600, 630)))
        proposal = propose(Snapshot(needs=(second, first)))

        # Сначала группа с меньшим выбором: у первой одно занятие, вторая уходит на 10:00.
        assert times(proposal, 1) == [(day, 540) for day in DAYS]
        assert times(proposal, 2) == [(day, 600) for day in DAYS]

    def test_frequency_more_than_sessions(self):
        proposal = propose(Snapshot(needs=(group(per_day=3, sessions=((1, 540, 570),)),)))
        assert len(times(proposal, 1)) == 3
        assert codes(proposal) == [IssueCode.GROUP_FREQUENCY]
        assert "в расписании групп 1 занятие" in proposal.conflicts[0].message

    def test_two_per_day(self):
        need = group(per_day=2, sessions=((1, 540, 570), (2, 600, 630), (3, 660, 690)))
        proposal = propose(Snapshot(needs=(need,)))
        assert times(proposal, 1).count((DAYS[0], 540)) == 1
        assert len(times(proposal, 1)) == 6

    def test_pinned_unit_is_kept(self):
        need = group(pinned_units=((DAYS[0], 1),))
        proposal = propose(Snapshot(needs=(need,)))
        assert times(proposal, 1) == [(DAYS[1], 540), (DAYS[2], 540)]

    def test_pool_without_group_and_no_sessions(self):
        pool = Need(1, 1, Kind.POOL, "Бассейн", DAYS, group_required=True)
        empty = group(2, sessions=())
        proposal = propose(Snapshot(needs=(pool, empty)))

        assert proposal.placements == []
        # Сначала группы ЛФК, потом бассейн.
        assert codes(proposal) == [IssueCode.NO_SESSIONS, IssueCode.POOL_TYPE_REQUIRED]


class TestEquipment:
    def test_least_loaded_start(self):
        load = tuple(EquipmentLoad(1, day, 780, 795) for day in DAYS)
        proposal = propose(Snapshot(needs=(equipment(),), equipment_load=load))
        assert times(proposal, 10) == [(day, 795) for day in DAYS]

    def test_capacity_and_patient_busy(self):
        load = (EquipmentLoad(1, DAYS[0], 780, 795),)
        busy = (Busy(DAYS[0], 795, 830),)
        proposal = propose(Snapshot(needs=(equipment(),), patient_busy=busy, equipment_load=load))

        assert (DAYS[0], 780) not in times(proposal, 10)
        assert codes(proposal) == [IssueCode.EQUIPMENT_UNPLACED]
        assert "06.10 — нет свободного времени" in proposal.conflicts[0].message

    def test_equipment_does_not_overlap_group(self):
        need = group(1, sessions=((1, 780, 810),))
        proposal = propose(Snapshot(needs=(equipment(), need)))
        assert times(proposal, 10) == [(day, 810) for day in DAYS]

    def test_unavailable_and_no_window(self):
        off = equipment(10, unavailable="тренажёр «st-150» выключен в справочнике")
        narrow = equipment(11, starts=())
        proposal = propose(Snapshot(needs=(off, narrow)))

        assert proposal.placements == []
        assert [i.message for i in proposal.issues] == [
            "st-150: тренажёр «st-150» выключен в справочнике.",
            "st-150: занятие не помещается в окно тренажёра.",
        ]

    def test_same_issue_not_repeated(self):
        busy = tuple(Busy(day, 700, 900) for day in DAYS)
        proposal = propose(Snapshot(needs=(equipment(per_day=2),), patient_busy=busy))
        assert codes(proposal) == [IssueCode.EQUIPMENT_UNPLACED]
