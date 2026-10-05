"""Смены и занятость инструкторов без Django (TZ.md, FR-STF-2…5)."""

from datetime import date, time, timedelta

import pytest

from apps.staff.domain import (
    Block,
    Busy,
    Duty,
    DutyKind,
    InstructorCalendar,
    InstructorDay,
    Pattern,
    ShiftOverride,
    ShiftRule,
    Slot,
    Team,
    TeamMember,
    busy_slots,
    covered_slots,
    in_period,
    is_working,
    pattern_works,
    periods_overlap,
    team_candidates,
)

ANCHOR = date(2026, 10, 1)  # четверг
MON = date(2026, 10, 5)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]


class TestPatterns:
    def test_two_two_cycle_from_anchor(self):
        works = [pattern_works(Pattern.TWO_TWO, ANCHOR, day) for day in days(ANCHOR, 8)]
        assert works == [True, True, False, False, True, True, False, False]

    def test_two_two_before_anchor(self):
        # Цикл идёт и назад: за 2 и 1 день до якоря — выходные, за 4 и 3 — рабочие.
        before = [pattern_works("2/2", ANCHOR, ANCHOR - timedelta(days=n)) for n in (1, 2, 3, 4)]
        assert before == [False, False, True, True]

    def test_two_two_ignores_weekdays(self):
        anchor = SAT
        assert pattern_works(Pattern.TWO_TWO, anchor, SAT)
        assert pattern_works(Pattern.TWO_TWO, anchor, SUN)

    def test_five_two_is_monday_to_friday(self):
        week = [pattern_works(Pattern.FIVE_TWO, ANCHOR, day) for day in days(MON, 7)]
        assert week == [True] * 5 + [False] * 2

    def test_daily(self):
        assert all(pattern_works(Pattern.DAILY, ANCHOR, day) for day in days(MON, 7))

    def test_unknown_pattern(self):
        with pytest.raises(ValueError):
            pattern_works("3/3", ANCHOR, MON)


class TestPeriods:
    def test_in_period_inclusive_bounds(self):
        assert in_period(MON, MON, MON)
        assert not in_period(MON - timedelta(days=1), MON, None)
        assert in_period(date(2030, 1, 1), MON, None)
        assert not in_period(SAT, MON, SAT - timedelta(days=1))

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ((MON, SAT), (SAT, None), True),  # общий день на границе
            ((MON, SAT - timedelta(days=1)), (SAT, None), False),
            ((SAT, None), (MON, None), True),
            ((MON, MON), (MON, MON), True),
            ((SAT, SUN), (MON, MON), False),
        ],
    )
    def test_overlap(self, a, b, expected):
        assert periods_overlap(*a, *b) is expected
        assert periods_overlap(*b, *a) is expected


class TestIsWorking:
    def test_no_pattern_means_day_off(self):
        assert not is_working([], [], MON)

    def test_pattern_period_bounds(self):
        rule = ShiftRule(Pattern.DAILY, ANCHOR, valid_from=MON, valid_to=SAT)
        assert not is_working([rule], [], MON - timedelta(days=1))
        assert is_working([rule], [], MON)
        assert is_working([rule], [], SAT)
        assert not is_working([rule], [], SUN)

    def test_pattern_change_from_date(self):
        # С понедельника 12.10 инструктор переходит с 5/2 на каждый день.
        old = ShiftRule(Pattern.FIVE_TWO, ANCHOR, valid_from=ANCHOR, valid_to=SUN)
        new = ShiftRule(Pattern.DAILY, ANCHOR, valid_from=SUN + timedelta(days=1))
        rules = [old, new]
        assert not is_working(rules, [], SAT)
        assert is_working(rules, [], SAT + timedelta(days=7))

    def test_exception_beats_pattern(self):
        rule = ShiftRule(Pattern.FIVE_TWO, ANCHOR, valid_from=ANCHOR)
        sick = ShiftOverride(MON, is_working=False)
        swap = ShiftOverride(SAT, is_working=True)
        assert not is_working([rule], [sick, swap], MON)
        assert is_working([rule], [sick, swap], SAT)
        assert is_working([rule], [sick, swap], MON + timedelta(days=1))

    def test_exception_without_pattern(self):
        assert is_working([], [ShiftOverride(MON, True)], MON)


SLOT_910, SLOT_1030, SLOT_1110, SLOT_1300 = 1, 3, 4, 5


class TestBusySlots:
    def duties(self):
        return [
            Duty(SLOT_910, DutyKind.GROUP_LEAD, ANCHOR, label="Эрго общая"),
            Duty(SLOT_1300, DutyKind.BOS, ANCHOR, valid_to=SAT),
            Duty(SLOT_1110, DutyKind.METHOD_WORK, SAT),
        ]

    def test_duties_in_period(self):
        busy = busy_slots(self.duties(), [], MON)
        assert busy == {
            SLOT_910: Busy(DutyKind.GROUP_LEAD, "Эрго общая"),
            SLOT_1300: Busy(DutyKind.BOS),
        }
        assert set(busy_slots(self.duties(), [], SAT)) == {SLOT_910, SLOT_1300, SLOT_1110}
        assert set(busy_slots(self.duties(), [], SUN)) == {SLOT_910, SLOT_1110}

    def test_day_off_has_no_duties(self):
        assert busy_slots(self.duties(), [], MON, working=False) == {}

    def test_block_replaces_and_adds(self):
        blocks = [
            Block(MON, SLOT_910, DutyKind.OTHER, "Совещание"),
            Block(MON, SLOT_1030, DutyKind.METHOD_WORK),
            Block(SUN, SLOT_1030, DutyKind.BOS),  # другой день — не влияет
        ]
        busy = busy_slots(self.duties(), blocks, MON)
        assert busy[SLOT_910] == Busy(DutyKind.OTHER, "Совещание", one_off=True)
        assert busy[SLOT_1030].text == "Метод. работа"
        assert busy[SLOT_1030].one_off

    def test_clear_frees_slot_only_that_day(self):
        blocks = [Block(MON, SLOT_910, DutyKind.CLEAR), Block(MON, SLOT_1030, DutyKind.CLEAR)]
        assert SLOT_910 not in busy_slots(self.duties(), blocks, MON)
        assert SLOT_1030 not in busy_slots(self.duties(), blocks, MON)
        assert SLOT_910 in busy_slots(self.duties(), blocks, MON + timedelta(days=1))

    def test_busy_text(self):
        assert Busy(DutyKind.GROUP_LEAD, "I can нога").text == "I can нога"
        assert Busy(DutyKind.BOS).text == "БОС"
        assert Busy(DutyKind.METHOD_WORK).text == "Метод. работа"


class TestCalendar:
    def calendar(self) -> InstructorCalendar:
        return InstructorCalendar(
            patterns=(ShiftRule(Pattern.TWO_TWO, MON, valid_from=ANCHOR),),
            exceptions=(ShiftOverride(MON + timedelta(days=1), False),),
            duties=(Duty(SLOT_910, DutyKind.METHOD_WORK, ANCHOR),),
            blocks=(Block(MON, SLOT_1300, DutyKind.BOS),),
        )

    def test_working_day(self):
        day = self.calendar().day(MON)
        assert day.working
        assert not day.is_free(SLOT_910)
        assert not day.is_free(SLOT_1300)
        assert day.is_free(SLOT_1030)
        assert day.free_slots([SLOT_910, SLOT_1030, SLOT_1110, SLOT_1300]) == [
            SLOT_1030,
            SLOT_1110,
        ]

    def test_sick_day_has_no_free_slots(self):
        calendar = self.calendar()
        sick = MON + timedelta(days=1)
        assert calendar.by_pattern(sick)
        assert not calendar.is_working(sick)
        assert calendar.exception_on(sick) == ShiftOverride(sick, False)
        assert calendar.day(sick) == InstructorDay(sick, False, {})
        assert calendar.day(sick).free_slots([SLOT_1030]) == []

    def test_day_off_by_pattern(self):
        calendar = self.calendar()
        off = MON + timedelta(days=2)
        assert not calendar.is_working(off)
        assert calendar.exception_on(off) is None
        assert calendar.pattern_on(off) == calendar.patterns[0]
        assert calendar.pattern_on(ANCHOR - timedelta(days=1)) is None

    def test_empty_calendar(self):
        assert not InstructorCalendar().day(MON).is_free(SLOT_910)


GRID = (
    Slot(SLOT_910, time(9, 10), time(9, 40)),
    Slot(2, time(9, 50), time(10, 20)),
    Slot(SLOT_1030, time(10, 30), time(11, 0)),
    Slot(SLOT_1110, time(11, 10), time(11, 40)),
    Slot(SLOT_1300, time(13, 0), time(13, 30)),
)
SLOT_950 = 2


class TestGroupSpan:
    def test_covered_slots(self):
        assert covered_slots(SLOT_910, None, GRID) == {SLOT_910}
        assert covered_slots(SLOT_910, (time(9, 10), time(10, 10)), GRID) == {SLOT_910, SLOT_950}
        # Лого-тренинг 11:20–11:50: только 11:10, слоты касанием не задеваются.
        assert covered_slots(SLOT_1110, (time(11, 20), time(11, 50)), GRID) == {SLOT_1110}
        assert covered_slots(SLOT_910, (time(9, 40), time(9, 50)), GRID) == {SLOT_910}

    def test_long_group_duty_takes_both_slots(self):
        duty = Duty(
            SLOT_910, DutyKind.GROUP_LEAD, ANCHOR, label="Эрго", span=(time(9, 10), time(10, 10))
        )
        busy = busy_slots([duty], [], MON, slots=GRID)
        assert set(busy) == {SLOT_910, SLOT_950}
        assert busy[SLOT_950].text == "Эрго"

    def test_clear_in_any_slot_removes_whole_group(self):
        duty = Duty(SLOT_910, DutyKind.GROUP_LEAD, ANCHOR, span=(time(9, 10), time(10, 10)))
        other = Duty(SLOT_1030, DutyKind.BOS, ANCHOR)
        busy = busy_slots([duty, other], [Block(MON, SLOT_950, DutyKind.CLEAR)], MON, slots=GRID)
        assert set(busy) == {SLOT_1030}

    def test_one_off_group_block_spans_and_replaces(self):
        duties = [
            Duty(SLOT_950, DutyKind.METHOD_WORK, ANCHOR),
            Duty(SLOT_1030, DutyKind.BOS, ANCHOR),
        ]
        block = Block(MON, SLOT_910, DutyKind.GROUP_LEAD, "Нейро", span=(time(9, 10), time(10, 10)))
        busy = busy_slots(duties, [block], MON, slots=GRID)
        assert busy[SLOT_910] == busy[SLOT_950] == Busy(DutyKind.GROUP_LEAD, "Нейро", one_off=True)
        assert busy[SLOT_1030] == Busy(DutyKind.BOS)


class TestCalendarRange:
    def test_outside_range_raises(self):
        calendar = InstructorCalendar(start=MON, end=SAT)
        calendar.day(MON)
        calendar.is_working(SAT)
        for day in (MON - timedelta(days=1), SUN):
            with pytest.raises(ValueError):
                calendar.day(day)
            with pytest.raises(ValueError):
                calendar.exception_on(day)


class TestTeams:
    def test_team_label_key_and_order(self):
        kim = TeamMember(8, "Ким", 20)
        parshukov = TeamMember(3, "Паршуков", 10)
        team = Team.of(kim, parshukov)
        assert team.label == "Паршуков/ Ким"
        assert team.key == "3-8"
        assert team.member_ids == (3, 8)
        assert Team.of(TeamMember(5, "Орлова")).label == "Орлова"
        with pytest.raises(ValueError):
            Team.of()

    def test_candidates(self):
        first, second = TeamMember(1, "А"), TeamMember(2, "Б")
        team = Team.of(first, second)
        works = InstructorCalendar(patterns=(ShiftRule(Pattern.DAILY, ANCHOR, ANCHOR),))
        busy = InstructorCalendar(
            patterns=(ShiftRule(Pattern.DAILY, ANCHOR, ANCHOR),),
            duties=(Duty(SLOT_910, DutyKind.BOS, ANCHOR),),
        )
        assert team_candidates(team, {1: works, 2: works}, MON, SLOT_910) == [1, 2]
        assert team_candidates(team, {1: busy, 2: works}, MON, SLOT_910) == [2]
        assert team_candidates(team, {1: busy, 2: InstructorCalendar()}, MON, SLOT_910) == []
        assert team_candidates(team, {2: works}, MON, SLOT_910) == [2]
