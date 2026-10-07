"""Расчёт шахматки на день (TZ.md FR-SCH-6…8): перенос, автоматическая постановка, блоки."""

from apps.scheduling.domain.board_day import (
    Column,
    GridSlot,
    Member,
    Patient,
    PatientDay,
    Seat,
    plan_day,
)

# Сетка: 9:10, 9:50, 10:30, 11:10, вечер 18:00 и 18:40.
SLOTS = [
    GridSlot(1, 550, 580),
    GridSlot(2, 590, 620),
    GridSlot(3, 630, 660),
    GridSlot(4, 670, 700),
    GridSlot(11, 1080, 1110, evening=True),
    GridSlot(12, 1120, 1150, evening=True),
]
DAY = frozenset({1, 2, 3, 4})
ALL = DAY | {11, 12}


def member(pk: int, free: frozenset[int] = DAY, *, working: bool = True, evening: bool = False):
    return Member(pk, working, free if working else frozenset(), evening)


def single(pk: int, **kwargs) -> Column:
    return Column(str(pk), (member(pk, **kwargs),))


def pair(a: Member, b: Member) -> Column:
    return Column(f"{a.id}-{b.id}", (a, b))


def plan(patients, columns, **kwargs) -> dict[int, PatientDay]:
    return {item.program_id: item for item in plan_day(patients, columns, SLOTS, **kwargs)}


class TestCarry:
    def test_same_instructor_and_time_with_note(self):
        result = plan([Patient(1, 1, seats=(Seat(10, 3, "art"),))], [single(10)])
        assert result[1] == PatientDay(1, (Seat(10, 3, "art"),))

    def test_partner_continues_when_instructor_is_off(self):
        """Пара 2/2 — одна колонка: работает напарник, пациент остаётся (FR-SCH-6, п. 3)."""
        columns = [pair(member(10, working=False), member(20))]
        result = plan([Patient(1, 1, seats=(Seat(10, 2),))], columns)
        assert result[1].seats == (Seat(20, 2),)

    def test_instructor_off_sends_to_unplaced(self):
        result = plan([Patient(1, 1, seats=(Seat(10, 2),))], [single(10, working=False)])
        assert result[1] == PatientDay(1, (), unplaced=1)

    def test_slot_closed_by_duty_sends_to_unplaced(self):
        columns = [single(10, free=frozenset({1, 3}))]
        result = plan([Patient(1, 1, seats=(Seat(10, 2),))], columns)
        assert result[1].unplaced == 1 and result[1].seats == ()

    def test_patient_busy_with_group_sends_to_unplaced(self):
        result = plan([Patient(1, 1, seats=(Seat(10, 2),), busy=((600, 630),))], [single(10)])
        assert result[1].unplaced == 1

    def test_unknown_instructor_or_slot_sends_to_unplaced(self):
        patient = Patient(1, 2, seats=(Seat(99, 1), Seat(10, 77)))
        result = plan([patient], [single(10)], auto=False)
        assert result[1] == PatientDay(1, (), unplaced=2)

    def test_several_patients_in_one_cell_are_kept(self):
        """Вручную в ячейку ставят сколько угодно пациентов — перенос их не разводит."""
        patients = [Patient(n, 1, seats=(Seat(10, 1),)) for n in (1, 2, 3)]
        result = plan(patients, [single(10)])
        assert all(result[n].seats == (Seat(10, 1),) for n in (1, 2, 3))


class TestNeed:
    def test_no_lessons_this_day_frees_the_cell(self):
        """Выписка или отменённое индивидуальное: пациента в шахматке нет, блоков тоже."""
        patient = Patient(1, 0, seats=(Seat(10, 1),), unplaced=1, cancelled=1)
        assert plan([patient], [single(10)])[1] == PatientDay(1)

    def test_fewer_lessons_keep_cells_first(self):
        patient = Patient(1, 1, seats=(Seat(10, 3), Seat(10, 1)), cancelled=1)
        assert plan([patient], [single(10)])[1] == PatientDay(1, (Seat(10, 1),))

    def test_cancelled_is_carried_and_not_placed_automatically(self):
        result = plan([Patient(1, 2, seats=(Seat(10, 1),), cancelled=1)], [single(10)])
        assert result[1] == PatientDay(1, (Seat(10, 1),), cancelled=1)

    def test_unplaced_stays_unplaced(self):
        """«Не распределены» распределяет специалист, а не перенос (FR-SCH-11)."""
        result = plan([Patient(1, 1, unplaced=1)], [single(10)])
        assert result[1] == PatientDay(1, (), unplaced=1)

    def test_without_auto_missing_goes_to_unplaced(self):
        """Сегодняшняя шахматка: новые занятия не ставятся сами (FR-SCH-9)."""
        result = plan([Patient(1, 2, seats=(Seat(10, 1),))], [single(10)], auto=False)
        assert result[1] == PatientDay(1, (Seat(10, 1),), unplaced=1)


class TestAuto:
    def test_least_loaded_column_first(self):
        """Равномерность: при окнах у нескольких инструкторов новый идёт к тому, у кого меньше
        пациентов в этот день, а не к первому (FR-SCH-7)."""
        fixed = (Seat(10, 1), Seat(10, 2))
        result = plan([Patient(1, 1)], [single(10), single(20)], fixed=fixed)
        assert result[1].seats == (Seat(20, 1),)

    def test_spread_over_instructors(self):
        patients = [Patient(n, 1) for n in range(1, 5)]
        result = plan(patients, [single(10), single(20)])
        instructors = [result[n].seats[0].instructor_id for n in range(1, 5)]
        assert sorted(instructors) == [10, 10, 20, 20]

    def test_tie_earlier_slot_then_column_order(self):
        columns = [single(10, free=frozenset({2, 3})), single(20, free=frozenset({2}))]
        assert plan([Patient(1, 1)], columns)[1].seats == (Seat(10, 2),)

    def test_never_two_in_a_cell_automatically(self):
        fixed = tuple(Seat(10, slot) for slot in DAY)
        result = plan([Patient(1, 1)], [single(10)], fixed=fixed)
        assert result[1] == PatientDay(1, (), unplaced=1)

    def test_not_in_a_row_and_not_over_group(self):
        """Два индивидуальных пациента в день — не подряд; во время группы — нет."""
        patient = Patient(1, 2, busy=((550, 580),))
        seats = plan([patient], [single(10), single(20)])[1].seats
        assert {s.slot_id for s in seats} == {2, 4}

    def test_off_and_evening_slots_are_not_used_for_ordinary_lesson(self):
        columns = [single(10, working=False), single(20, free=frozenset({11}), evening=True)]
        assert plan([Patient(1, 1)], columns)[1] == PatientDay(1, (), unplaced=1)


class TestEvening:
    def test_moto_l_goes_to_evening_slot_of_two_two(self):
        """Мото-Л: одно индивидуальное — вечером у инструктора 2/2, с пометкой (FR-SCH-8)."""
        columns = [single(10, free=ALL), single(20, free=ALL, evening=True)]
        seats = plan([Patient(1, 2, evening_note="Мото-Л")], columns)[1].seats
        assert Seat(20, 11, "Мото-Л") in seats
        assert len(seats) == 2 and any(s.slot_id in DAY and s.note == "" for s in seats)

    def test_no_evening_window_sends_to_unplaced(self):
        result = plan([Patient(1, 1, evening_note="Мото-Л")], [single(10, free=ALL)])
        assert result[1] == PatientDay(1, (), unplaced=1)

    def test_evening_already_carried(self):
        columns = [single(20, free=ALL, evening=True)]
        patient = Patient(1, 2, seats=(Seat(20, 11, "Мото-Л"),), evening_note="Мото-Л")
        seats = plan([patient], columns)[1].seats
        assert seats[0] == Seat(20, 11, "Мото-Л") and seats[1].slot_id in DAY
