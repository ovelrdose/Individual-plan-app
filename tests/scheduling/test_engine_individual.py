"""Шаг 3 движка (TZ.md §7.3): индивидуальные занятия — без БД, вымышленные инструкторы."""

from dataclasses import replace
from datetime import date

from apps.scheduling.domain import (
    Assignment,
    Busy,
    IssueCode,
    Need,
    Proposal,
    Slot,
    Snapshot,
    Staff,
    is_weekend,
    propose,
)
from apps.scheduling.domain.model import Kind
from apps.staff.domain import Team, TeamMember

from .test_engine import COURSE as FULL_COURSE
from .test_engine import equipment, group, m

# Инструктор подбирается только на будни (решение 56): логика команд проверяется на них,
# выходные — отдельными тестами в конце модуля.
COURSE = tuple(day for day in FULL_COURSE if not is_weekend(day))

SLOTS = (
    Slot(1, m(9, 10), m(9, 40)),
    Slot(2, m(9, 50), m(10, 20)),
    Slot(3, m(10, 30), m(11)),
    Slot(4, m(11, 10), m(11, 40)),
    Slot(5, m(13), m(13, 30)),
    Slot(6, m(13, 40), m(14, 10)),
    Slot(7, m(14, 20), m(14, 50)),
    Slot(8, m(15), m(15, 30)),
    Slot(9, m(15, 40), m(16, 10)),
    Slot(10, m(16, 20), m(16, 50)),
    Slot(11, m(18), m(18, 30), evening=True),
)
SLOT = {slot.id: slot for slot in SLOTS}
VOLKOV, LEBEDEVA, SOKOLOV, MOROZOVA = 1, 2, 3, 4
MEMBERS = {
    VOLKOV: TeamMember(VOLKOV, "Волков", 10),
    LEBEDEVA: TeamMember(LEBEDEVA, "Лебедева", 40),
    SOKOLOV: TeamMember(SOKOLOV, "Соколов", 30),
    MOROZOVA: TeamMember(MOROZOVA, "Морозова", 50),
}
TEAMS = (
    Team.of(MEMBERS[VOLKOV], MEMBERS[LEBEDEVA]),
    Team.of(MEMBERS[SOKOLOV]),
    Team.of(MEMBERS[MOROZOVA]),
)
# 2/2: Волков работает 1-й и 2-й день цикла, Лебедева — 3-й и 4-й; Соколов и Морозова — каждый день.
WORKING = {
    VOLKOV: {day for i, day in enumerate(COURSE) if i % 4 in (0, 1)},
    LEBEDEVA: {day for i, day in enumerate(COURSE) if i % 4 in (2, 3)},
    SOKOLOV: set(COURSE),
    MOROZOVA: set(COURSE),
}
INDIVIDUAL_ID = 7


def individual(pid: int = INDIVIDUAL_ID, *, per_day: int = 1, dates=COURSE, pinned_units=()):
    return Need(pid, 50, Kind.INDIVIDUAL, "Индивидуальное занятие", dates, per_day=per_day,
                pinned_units=pinned_units)  # fmt: skip


def staff(
    working: dict[int, set[date]] | None = None,
    *,
    busy: set[tuple[date, int, int]] = frozenset(),
    teams: tuple[Team, ...] = TEAMS,
    **fields,
) -> Staff:
    """Свободно всё, где инструктор работает, кроме ``busy`` (дата, слот, инструктор)."""
    working = WORKING if working is None else working
    free = frozenset(
        (day, slot.id, pk)
        for pk, days in working.items()
        for day in days
        for slot in SLOTS
        if (day, slot.id, pk) not in busy
    )
    fields.setdefault(
        "working", frozenset((day, pk) for pk, days in working.items() for day in days)
    )
    return Staff(slots=SLOTS, teams=teams, free=free, **fields)


def placed(proposal: Proposal, pid: int = INDIVIDUAL_ID) -> list:
    return [p for p in proposal.placements if p.prescription_id == pid]


def codes(proposal: Proposal) -> list[IssueCode]:
    return [issue.code for issue in proposal.issues]


def test_scenario_1_everything_placed_same_time_every_day():
    snapshot = Snapshot(
        needs=(
            group(1, "Эрго общая", m(9, 10)),
            group(2, "I can нога", m(10, 30)),
            individual(),
            equipment(3),
        ),
        staff=staff(),
    )

    proposal = propose(snapshot)

    assert proposal.issues == []
    items = placed(proposal)
    assert [p.date for p in items] == list(COURSE)
    # 9:10 занят группой — первый свободный слот, у пары (раньше в шахматке).
    assert {(p.start, p.end, p.slot_id) for p in items} == {(m(9, 50), m(10, 20), 2)}
    assert {p.instructor_id for p in items} == {VOLKOV, LEBEDEVA}
    assert {p.start for p in proposal.placements if p.prescription_id == 3} == {m(13)}


def test_scenario_3_pair_two_two_same_time_working_member_each_day():
    proposal = propose(Snapshot(needs=(individual(),), staff=staff()))

    items = placed(proposal)
    assert {p.slot_id for p in items} == {1}
    for p in items:
        assert p.date in WORKING[p.instructor_id], "в дату стоит работающий член пары"
    assert {p.instructor_id for p in items} == {VOLKOV, LEBEDEVA}
    assert proposal.issues == []


def test_scenario_4_three_patients_spread_over_teams():
    taken: set[tuple[date, int, int]] = set()
    load: dict[int, int] = {}
    chosen = []
    for _ in range(3):
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(busy=taken, load=tuple(sorted(load.items()))),
            )
        )
        items = placed(proposal)
        assert len(items) == len(COURSE)
        chosen.append(frozenset(p.instructor_id for p in items))
        for p in items:
            taken.add((p.date, p.slot_id, p.instructor_id))
            load[p.instructor_id] = load.get(p.instructor_id, 0) + 1

    assert chosen == [
        frozenset({VOLKOV, LEBEDEVA}),
        frozenset({SOKOLOV}),
        frozenset({MOROZOVA}),
    ]


def test_scenario_5_pool_interval_blocks_overlapping_slot():
    pool = group(1, "ЛФК в воде: нижняя конечность", m(9, 45), kind=Kind.POOL)
    only = {SOKOLOV: set(COURSE)}
    busy = {(day, slot.id, SOKOLOV) for day in COURSE for slot in SLOTS if slot.id not in (2, 3)}

    proposal = propose(
        Snapshot(needs=(pool, individual()), staff=staff(only, busy=busy, teams=TEAMS[1:2]))
    )

    assert {p.start for p in placed(proposal)} == {m(10, 30)}, "9:50–10:20 пересекается с 9:45"


def test_scenario_6_everything_busy_on_one_date_conflict_only_there():
    day = COURSE[3]
    busy = {(day, slot.id, pk) for slot in SLOTS for pk in MEMBERS}

    proposal = propose(Snapshot(needs=(individual(),), staff=staff(busy=busy)))

    assert [p.date for p in placed(proposal)] == [d for d in COURSE if d != day]
    assert codes(proposal) == [IssueCode.INDIVIDUAL_UNPLACED]
    assert proposal.issues[0].message == (
        "Индивидуальное занятие: 08.10 — нет свободного инструктора, не поставлено."
    )
    assert proposal.issues[0].is_conflict


def test_scenario_8_pinned_unit_is_not_doubled():
    day = COURSE[0]
    snapshot = Snapshot(
        needs=(individual(pinned_units=((day, 1),)),),
        patient_busy=(Busy(day, m(15), m(15, 30), individual=True),),
        staff=staff(),
    )

    proposal = propose(snapshot)

    assert day not in {p.date for p in placed(proposal)}
    assert len(placed(proposal)) == len(COURSE) - 1


def test_scenario_9_per_day_above_limit_is_capped_with_warning():
    proposal = propose(Snapshot(needs=(individual(per_day=3),), staff=staff(max_per_day=2)))

    per_day = {day: 0 for day in COURSE}
    for p in placed(proposal):
        per_day[p.date] += 1
    assert set(per_day.values()) == {2}
    assert codes(proposal) == [IssueCode.INDIVIDUAL_LIMIT]
    assert not proposal.conflicts


def test_scenario_10_slot_taken_elsewhere_is_not_offered():
    # Слот 9:10 у всех занят пациентами других программ (в том числе других отделений):
    # сервис не включает его в free — движок туда не ставит.
    busy = {(day, 1, pk) for day in COURSE for pk in MEMBERS}

    proposal = propose(Snapshot(needs=(individual(),), staff=staff(busy=busy)))

    assert {p.slot_id for p in placed(proposal)} == {2}


def test_scenario_11_two_per_day_never_adjacent():
    busy = {(day, slot.id, pk) for day in COURSE for slot in SLOTS for pk in MEMBERS if slot.id > 2}

    proposal = propose(Snapshot(needs=(individual(per_day=2),), staff=staff(busy=busy)))

    items = placed(proposal)
    assert [p.date for p in items] == list(COURSE), "одно занятие в день, второе — не подряд"
    assert IssueCode.INDIVIDUAL_UNPLACED in codes(proposal)


def test_lunch_breaks_adjacency():
    # 11:10–11:40 и 13:00–13:30 — до и после обеда, соседними не считаются (§7.2).
    busy = {
        (day, slot.id, pk)
        for day in COURSE
        for slot in SLOTS
        for pk in MEMBERS
        if slot.id not in (4, 5)
    }

    proposal = propose(Snapshot(needs=(individual(per_day=2),), staff=staff(busy=busy)))

    assert {p.slot_id for p in placed(proposal)} == {4, 5}
    assert len(placed(proposal)) == 2 * len(COURSE)
    assert proposal.issues == []


def test_adjacent_to_pinned_individual_is_skipped():
    day = COURSE[0]
    snapshot = Snapshot(
        needs=(individual(dates=(day,)),),
        patient_busy=(Busy(day, m(9, 10), m(9, 40), individual=True),),
        staff=staff(),
    )

    proposal = propose(snapshot)

    assert [p.slot_id for p in placed(proposal)] == [3], "9:50 — сразу после 9:10, нельзя"


def test_evening_slots_are_never_used():
    busy = {
        (day, slot.id, pk) for day in COURSE for slot in SLOTS for pk in MEMBERS if slot.id < 11
    }

    proposal = propose(Snapshot(needs=(individual(),), staff=staff(busy=busy)))

    assert placed(proposal) == []
    assert codes(proposal) == [IssueCode.INDIVIDUAL_UNPLACED]


def test_no_instructors_means_unplaced():
    proposal = propose(Snapshot(needs=(individual(dates=COURSE[:2]),), staff=Staff(slots=SLOTS)))

    assert placed(proposal) == []
    assert proposal.issues[0].message == (
        "Индивидуальное занятие: 05.10, 06.10 — нет свободного инструктора, не поставлено."
    )


def every_slot_fails_once(who=MEMBERS) -> set[tuple[date, int, int]]:
    """Слот k занят у ``who`` в k-й день курса: покрытие всех слотов одинаковое (14 из 15),
    основной выбор — самый ранний слот, а в день его занятости нужна замена."""
    return {(COURSE[slot.id], slot.id, pk) for slot in SLOTS[:10] for pk in who}


def test_day_replacement_same_slot_least_loaded_instructor():
    first, third = COURSE[0], COURSE[2]
    # У всех команд покрытие 14 из 15: пара — основной выбор (загрузка 0, раньше в шахматке).
    # В первый день Волков занят весь день, Лебедева не работает — замена в том же слоте 9:10:
    # из Соколова и Морозовой — менее загруженная Морозова.
    busy = {(first, slot.id, VOLKOV) for slot in SLOTS} | {
        (third, slot.id, pk) for slot in SLOTS for pk in (SOKOLOV, MOROZOVA)
    }
    proposal = propose(
        Snapshot(
            needs=(individual(),),
            staff=staff(busy=busy, load=((SOKOLOV, 5), (MOROZOVA, 1))),
        )
    )

    by_day = {p.date: p for p in placed(proposal)}
    assert (by_day[first].instructor_id, by_day[first].slot_id) == (MOROZOVA, 1)
    assert {
        (by_day[d].slot_id, by_day[d].instructor_id in (VOLKOV, LEBEDEVA)) for d in COURSE[1:]
    } == {(1, True)}
    assert [issue.message for issue in proposal.issues] == [
        "Индивидуальное занятие: 05.10 — Морозова вместо Волков/ Лебедева.",
    ]
    assert codes(proposal) == [IssueCode.DAY_DEVIATION]


def test_day_replacement_nearest_free_slot():
    proposal = propose(Snapshot(needs=(individual(),), staff=staff(busy=every_slot_fails_once())))

    by_day = {p.date: p for p in placed(proposal)}
    second = COURSE[1]
    assert (by_day[second].instructor_id, by_day[second].slot_id) == (VOLKOV, 2)
    assert {by_day[d].slot_id for d in COURSE if d != second} == {1}
    assert [issue.message for issue in proposal.issues] == [
        "Индивидуальное занятие: 06.10 — в 9:50 вместо 9:10.",
    ]


class TestPreferred:
    def test_preferred_on_working_days_partner_otherwise(self):
        proposal = propose(Snapshot(needs=(individual(),), staff=staff(preferred=VOLKOV)))

        items = placed(proposal)
        assert {p.slot_id for p in items} == {1}, "одно время на весь курс"
        for p in items:
            expected = VOLKOV if p.date in WORKING[VOLKOV] else LEBEDEVA
            assert p.instructor_id == expected
        assert codes(proposal) == [IssueCode.PREFERRED_REPLACED]
        assert "Лебедева вместо Волков (по желанию пациента)" in proposal.issues[0].message
        assert not proposal.conflicts

    def test_preferred_beats_balance(self):
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(preferred=MOROZOVA, load=((MOROZOVA, 100),)),
            )
        )

        assert {p.instructor_id for p in placed(proposal)} == {MOROZOVA}
        assert proposal.issues == []

    def test_preferred_busy_in_main_slot_moves_to_his_other_slot(self):
        day = COURSE[1]
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(busy=every_slot_fails_once((SOKOLOV,)), preferred=SOKOLOV),
            )
        )

        by_day = {p.date: p for p in placed(proposal)}
        assert {p.instructor_id for p in placed(proposal)} == {SOKOLOV}
        assert (by_day[day].instructor_id, by_day[day].slot_id) == (SOKOLOV, 2)
        assert codes(proposal) == [IssueCode.DAY_DEVIATION]

    def test_preferred_without_partner_replaced_by_anyone(self):
        day = COURSE[2]
        busy = {(day, slot.id, SOKOLOV) for slot in SLOTS}
        proposal = propose(
            Snapshot(needs=(individual(),), staff=staff(busy=busy, preferred=SOKOLOV))
        )

        by_day = {p.date: p for p in placed(proposal)}
        assert by_day[day].instructor_id != SOKOLOV and by_day[day].slot_id == 1
        assert proposal.issues[0].code == IssueCode.PREFERRED_REPLACED
        assert proposal.issues[0].message.startswith("Индивидуальное занятие: 07.10 — ")

    def test_preferred_never_available_falls_back_to_general_rule(self):
        working = {pk: days for pk, days in WORKING.items() if pk != MOROZOVA}
        proposal = propose(
            Snapshot(needs=(individual(),), staff=staff(working, preferred=MOROZOVA))
        )

        assert len(placed(proposal)) == len(COURSE)
        assert {p.slot_id for p in placed(proposal)} == {1}
        assert set(codes(proposal)) == {IssueCode.PREFERRED_REPLACED}


class TestPrevious:
    def previous(self, instructor: int, slot: int, dates=COURSE) -> tuple[Assignment, ...]:
        return tuple(Assignment(INDIVIDUAL_ID, day, instructor, slot) for day in dates)

    def test_previous_is_kept_even_if_not_best(self):
        proposal = propose(
            Snapshot(needs=(individual(),), staff=staff(previous=self.previous(SOKOLOV, 5)))
        )

        assert {(p.instructor_id, p.slot_id) for p in placed(proposal)} == {(SOKOLOV, 5)}
        assert proposal.issues == []

    def test_previous_no_longer_possible_is_replaced_only_on_that_date(self):
        day = COURSE[4]
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(busy={(day, 5, SOKOLOV)}, previous=self.previous(SOKOLOV, 5)),
            )
        )

        by_day = {p.date: p for p in placed(proposal)}
        assert all(
            (by_day[d].instructor_id, by_day[d].slot_id) == (SOKOLOV, 5) for d in COURSE if d != day
        )
        # Занят только инструктор: то же время у другого свободного (§7.3, порядок замены;
        # FR-SCH-13), а не новый выбор команды на курс по загрузке.
        assert by_day[day].slot_id == 5 and by_day[day].instructor_id != SOKOLOV
        assert [issue.message for issue in proposal.issues] == [
            "Индивидуальное занятие: 09.10 — Волков вместо Соколов."
        ]

    def test_team_busy_all_day_goes_to_anyone_in_same_slot(self):
        day = COURSE[4]
        busy = {(day, slot.id, SOKOLOV) for slot in SLOTS}
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(busy=busy, previous=self.previous(SOKOLOV, 5), load=((MOROZOVA, 9),)),
            )
        )

        by_day = {p.date: p for p in placed(proposal)}
        assert by_day[day].slot_id == 5 and by_day[day].instructor_id in (VOLKOV, LEBEDEVA)
        assert len(proposal.issues) == 1 and "09.10" in proposal.issues[0].message

    def test_moved_dates_stay_with_team_unchanged_dates_not_warned(self):
        # Пара в 9:10 весь курс; с 12.10 в 9:10 группа — эти даты уходят к паре в 9:50,
        # 05–11.10 не меняются и предупреждений по ним нет.
        later = COURSE[7:]
        new_group = group(1, "Эрго общая", m(9, 10), dates=later)
        previous = tuple(
            Assignment(INDIVIDUAL_ID, day, VOLKOV if day in WORKING[VOLKOV] else LEBEDEVA, 1)
            for day in COURSE
        )
        proposal = propose(
            Snapshot(
                needs=(new_group, individual()),
                staff=staff(previous=previous, load=((VOLKOV, 50), (LEBEDEVA, 50))),
            )
        )

        by_day = {p.date: p for p in placed(proposal)}
        assert {by_day[d].slot_id for d in COURSE[:7]} == {1}
        assert {by_day[d].slot_id for d in later} == {2}
        assert {by_day[d].instructor_id for d in later} <= {VOLKOV, LEBEDEVA}
        assert [issue.code for issue in proposal.issues] == [IssueCode.DAY_DEVIATION]
        assert "05.10" not in proposal.issues[0].message

    def test_new_prescription_does_not_take_slot_of_older_one(self):
        # Старое назначение (pk 9) держит Соколова в 9:10; новое (pk 8, меньший pk) подбирается
        # позже прошлых постановок всех назначений и слот не захватывает.
        previous = tuple(Assignment(9, day, SOKOLOV, 1) for day in COURSE)
        proposal = propose(
            Snapshot(
                needs=(individual(8), individual(9)),
                staff=staff(previous=previous, max_per_day=2),
            )
        )

        assert {(p.instructor_id, p.slot_id) for p in placed(proposal, 9)} == {(SOKOLOV, 1)}
        assert len(placed(proposal, 8)) == len(COURSE)
        assert 1 not in {p.slot_id for p in placed(proposal, 8)}
        assert proposal.issues == []

    def test_two_per_day_lost_early_seat_keeps_late_one(self):
        # 2 р/д: пара в 9:10 и Соколов в 13:00. В одну дату пара занята весь день —
        # 13:00 не трогается, ранняя постановка — к другому свободному в 9:10.
        day = COURSE[0]
        previous = tuple(
            Assignment(INDIVIDUAL_ID, d, VOLKOV if d in WORKING[VOLKOV] else LEBEDEVA, 1)
            for d in COURSE
        ) + self.previous(SOKOLOV, 5)
        busy = {(day, slot.id, VOLKOV) for slot in SLOTS}
        proposal = propose(
            Snapshot(
                needs=(individual(per_day=2),),
                staff=staff(busy=busy, previous=previous, load=((SOKOLOV, 3),)),
            )
        )

        seats = {(p.date, p.slot_id): p.instructor_id for p in placed(proposal)}
        assert all(seats[(d, 5)] == SOKOLOV for d in COURSE), "13:00 не трогается"
        assert seats[(day, 1)] == MOROZOVA
        assert len(placed(proposal)) == 2 * len(COURSE)
        assert [issue.message for issue in proposal.issues] == [
            "Индивидуальное занятие: 05.10 — Морозова вместо Волков/ Лебедева."
        ]

    def test_previous_overlapping_new_group_is_dropped(self):
        new_group = group(1, "Эрго общая", m(13))
        proposal = propose(
            Snapshot(
                needs=(new_group, individual()),
                staff=staff(previous=self.previous(SOKOLOV, 5)),
            )
        )

        assert 5 not in {p.slot_id for p in placed(proposal)}
        assert len(placed(proposal)) == len(COURSE)

    def test_new_dates_continue_previous_choice(self):
        # Курс продлили: прошлые постановки — на первые 10 дней, остальные добираются тем же.
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(previous=self.previous(SOKOLOV, 5, COURSE[:10])),
            )
        )

        assert {(p.instructor_id, p.slot_id) for p in placed(proposal)} == {(SOKOLOV, 5)}
        assert len(placed(proposal)) == len(COURSE)

    def test_previous_of_other_instructor_ignored_when_preferred(self):
        proposal = propose(
            Snapshot(
                needs=(individual(),),
                staff=staff(previous=self.previous(SOKOLOV, 5), preferred=MOROZOVA),
            )
        )

        assert {p.instructor_id for p in placed(proposal)} == {MOROZOVA}

    def test_previous_beyond_daily_units_is_not_kept(self):
        day = COURSE[0]
        previous = (
            Assignment(INDIVIDUAL_ID, day, SOKOLOV, 5),
            Assignment(INDIVIDUAL_ID, day, MOROZOVA, 8),
        )
        proposal = propose(
            Snapshot(needs=(individual(dates=(day,)),), staff=staff(previous=previous))
        )

        assert [(p.instructor_id, p.slot_id) for p in placed(proposal)] == [(SOKOLOV, 5)]


def test_limit_shared_between_two_individual_prescriptions():
    proposal = propose(
        Snapshot(
            needs=(individual(per_day=2), individual(8, dates=COURSE[:2])),
            staff=staff(max_per_day=2),
        )
    )

    assert len(placed(proposal)) == 2 * len(COURSE)
    assert placed(proposal, 8) == []
    assert proposal.issues[-1].code == IssueCode.INDIVIDUAL_LIMIT
    assert "05.10, 06.10" in proposal.issues[-1].message


def test_individual_goes_before_equipment():
    busy = {
        (day, slot.id, pk) for day in COURSE for slot in SLOTS for pk in MEMBERS if slot.id != 5
    }

    proposal = propose(Snapshot(needs=(equipment(3), individual()), staff=staff(busy=busy)))

    assert {p.start for p in placed(proposal)} == {m(13)}
    assert {p.start for p in proposal.placements if p.prescription_id == 3} == {m(13, 30)}


def test_deterministic():
    snapshot = Snapshot(
        needs=(group(1, "Эрго общая", m(9, 10)), individual(per_day=2)),
        staff=staff(preferred=LEBEDEVA, load=((SOKOLOV, 3),)),
    )

    first = propose(snapshot)
    second = propose(replace(snapshot))

    assert first.placements == second.placements
    assert first.issues == second.issues


def test_inactive_preferred_message_has_name():
    # Инструктор по желанию выключен: его нет среди команд, имя приходит в preferred_name.
    proposal = propose(
        Snapshot(
            needs=(individual(dates=COURSE[:1]),),
            staff=staff(preferred=99, preferred_name="Архипов"),
        )
    )

    assert proposal.issues[0].message == (
        "Индивидуальное занятие: 05.10 — Волков вместо Архипов (по желанию пациента)."
    )


def test_inactive_preferred_without_name():
    proposal = propose(Snapshot(needs=(individual(dates=COURSE[:1]),), staff=staff(preferred=99)))

    assert proposal.issues[0].message == (
        "Индивидуальное занятие: 05.10 — Волков вместо инструктора по желанию пациента."
    )


# --- Выходные: то же время без инструктора (решение 56) ------------------------------------

WEEKEND = tuple(day for day in FULL_COURSE if is_weekend(day))


def test_weekend_same_slot_without_instructor():
    proposal = propose(Snapshot(needs=(individual(dates=FULL_COURSE),), staff=staff()))

    items = placed(proposal)
    assert [p.date for p in items] == list(FULL_COURSE)
    assert {p.slot_id for p in items} == {1}
    assert {p.date for p in items if p.instructor_id is None} == set(WEEKEND)
    assert proposal.issues == []


def test_weekend_patient_busy_takes_nearest_slot_with_warning():
    saturday = WEEKEND[0]
    busy = (Busy(saturday, m(9), m(9, 45)),)

    proposal = propose(
        Snapshot(needs=(individual(dates=FULL_COURSE),), patient_busy=busy, staff=staff())
    )

    on_saturday = [p for p in placed(proposal) if p.date == saturday]
    assert [(p.slot_id, p.instructor_id) for p in on_saturday] == [(2, None)]
    assert [i.message for i in proposal.issues] == [
        "Индивидуальное занятие: 10.10 — в 9:50 вместо 9:10."
    ]


def test_weekend_is_not_preferred_replacement():
    proposal = propose(
        Snapshot(needs=(individual(dates=FULL_COURSE),), staff=staff(preferred=SOKOLOV))
    )

    assert {p.instructor_id for p in placed(proposal)} == {SOKOLOV, None}
    assert proposal.issues == []


def test_weekend_working_days_do_not_lower_average_load():
    """Средняя загрузка — на рабочий будний день: выходные смены пары её не разбавляют."""
    working = {pk: set(days) for pk, days in WORKING.items()}
    working[VOLKOV] |= set(WEEKEND)
    single = (Team.of(MEMBERS[VOLKOV]), Team.of(MEMBERS[SOKOLOV]))
    # Волков: 6 занятий на 15 дней (0,4) — меньше, чем у Соколова 5 на 11 будней (0,45);
    # по будням у Волкова 6 на 11 — больше, пациент уходит к Соколову.
    load = ((VOLKOV, 6), (SOKOLOV, 5))

    proposal = propose(
        Snapshot(
            needs=(individual(dates=FULL_COURSE),),
            staff=staff({VOLKOV: working[VOLKOV], SOKOLOV: set(COURSE)}, teams=single, load=load),
        )
    )

    assert {p.instructor_id for p in placed(proposal)} == {SOKOLOV, None}


def test_weekend_previous_seat_is_rebuilt_from_unit_time():
    """Прошлая постановка выходного не держится: время выходного — время единицы."""
    saturday = WEEKEND[0]
    previous = (
        *(Assignment(INDIVIDUAL_ID, day, SOKOLOV, 3) for day in COURSE),
        Assignment(INDIVIDUAL_ID, saturday, SOKOLOV, 7),
    )

    proposal = propose(
        Snapshot(needs=(individual(dates=FULL_COURSE),), staff=staff(previous=previous))
    )

    assert {(p.slot_id, p.instructor_id) for p in placed(proposal) if p.date in WEEKEND} == {
        (3, None)
    }


def test_weekend_counts_against_daily_limit_and_adjacency():
    proposal = propose(Snapshot(needs=(individual(dates=WEEKEND, per_day=2),), staff=staff()))

    for day in WEEKEND:
        starts = sorted(p.start for p in placed(proposal) if p.date == day)
        assert len(starts) == 2
        assert starts[1] - (starts[0] + 30) > 10, "два индивидуальных подряд"
