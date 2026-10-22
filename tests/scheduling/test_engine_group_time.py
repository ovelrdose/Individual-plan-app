"""Время группы на весь курс (TZ.md, решение 81): выбор, «авто» с предупреждением, прежнее
время и индивидуальные. Чистый Python, время — минуты от полуночи (495 = 8:15)."""

from datetime import date

from apps.scheduling.domain import Busy, IssueCode, Need, Session, Snapshot, propose
from apps.scheduling.domain.board_day import Column as BoardColumn
from apps.scheduling.domain.board_day import GridSlot, Member, Patient, Seat, plan_day
from apps.scheduling.domain.model import Kind

DAYS = (date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8))
EARLY = Session(1, 495, 525)  # 8:15
LATE = Session(2, 675, 705)  # 11:15
EVENING = Session(3, 900, 930)  # 15:00


def ds(*sessions: Session, **fields) -> Need:
    return Need(
        prescription_id=7,
        procedure_id=70,
        kind=Kind.DS_GROUP,
        label="ДС: острая спина",
        dates=DAYS,
        sessions=sessions,
        **fields,
    )


def starts(proposal) -> list[tuple[date, int]]:
    return sorted((p.date, p.start) for p in proposal.placements)


def codes(proposal) -> list[IssueCode]:
    return [issue.code for issue in proposal.issues]


class TestChosen:
    def test_chosen_wins_over_earlier(self):
        proposal = propose(Snapshot((ds(EARLY, LATE, chosen_session_id=2),)))

        assert starts(proposal) == [(day, 675) for day in DAYS]
        assert not proposal.issues

    def test_chosen_unavailable_day_is_deviation(self):
        """Выбранное время занято закреплённым в один день — замена на день, предупреждение."""
        busy = (Busy(DAYS[1], 680, 710),)
        proposal = propose(Snapshot((ds(EARLY, LATE, chosen_session_id=2),), patient_busy=busy))

        assert starts(proposal) == [(DAYS[0], 675), (DAYS[1], 495), (DAYS[2], 675)]
        assert codes(proposal) == [IssueCode.DAY_DEVIATION]
        assert "в 8:15 вместо 11:15" in proposal.issues[0].message

    def test_chosen_ignores_soft_busy(self):
        """Выбор человека важнее индивидуального: оно уйдёт в «Не распределены»."""
        soft = tuple(Busy(day, 670, 700) for day in DAYS)
        proposal = propose(Snapshot((ds(EARLY, LATE, chosen_session_id=2),), soft_busy=soft))

        assert starts(proposal) == [(day, 675) for day in DAYS]

    def test_chosen_ranks_like_single_session(self):
        """Выбранное время ставится раньше групп с бо́льшим выбором — как единственное."""
        other = Need(1, 10, Kind.DS_GROUP, "ДС: спина", DAYS, sessions=(LATE, EVENING))
        chosen = ds(EARLY, Session(4, 680, 710), chosen_session_id=4)

        proposal = propose(Snapshot((other, chosen)))

        by_need = {(p.prescription_id, p.start) for p in proposal.placements}
        assert (7, 680) in by_need and (1, 900) in by_need
        assert not proposal.issues


class TestAuto:
    def test_more_sessions_than_per_day_warns(self):
        proposal = propose(Snapshot((ds(EARLY, LATE, choice_needed=True),)))

        assert starts(proposal) == [(day, 495) for day in DAYS]
        assert codes(proposal) == [IssueCode.GROUP_TIME_AUTO]
        assert proposal.issues[0].message == (
            "ДС: острая спина: 2 занятия в день (8:15, 11:15) — время выбрано автоматически "
            "(8:15), выберите."
        )
        assert not proposal.issues[0].is_conflict
        assert proposal.warnings == proposal.issues

    def test_single_session_no_warning(self):
        proposal = propose(Snapshot((ds(EARLY),)))

        assert not proposal.issues

    def test_previous_time_does_not_jump(self):
        """Добавили более раннее занятие — пациенты остаются на прежнем времени."""
        proposal = propose(
            Snapshot((ds(EARLY, LATE, choice_needed=True, previous_session_ids=(2,)),))
        )

        assert starts(proposal) == [(day, 675) for day in DAYS]
        assert "(11:15), выберите" in proposal.issues[0].message

    def test_most_frequent_previous_first(self):
        need = ds(EARLY, LATE, EVENING, previous_session_ids=(3, 1))
        proposal = propose(Snapshot((need,)))

        assert starts(proposal) == [(day, 900) for day in DAYS]

    def test_soft_busy_is_avoided(self):
        """Без выбора группа обходит индивидуальное пациента в составленной шахматке."""
        soft = (Busy(DAYS[0], 500, 530),)
        proposal = propose(Snapshot((ds(EARLY, LATE),), soft_busy=soft))

        assert starts(proposal) == [(day, 675) for day in DAYS]

    def test_soft_busy_loses_to_coverage(self):
        """Обойти можно только при равном числе дней: закреплённое важнее."""
        hard = (Busy(DAYS[0], 680, 700),)
        soft = (Busy(DAYS[1], 500, 530),)
        proposal = propose(Snapshot((ds(EARLY, LATE),), patient_busy=hard, soft_busy=soft))

        assert starts(proposal) == [(day, 495) for day in DAYS]

    def test_twice_a_day_with_three_sessions_chosen_plus_auto(self):
        need = ds(EARLY, LATE, EVENING, per_day=2, chosen_session_id=3)
        proposal = propose(Snapshot((need,)))

        assert starts(proposal) == sorted((day, t) for day in DAYS for t in (495, 900))
        assert not proposal.issues

    def test_twice_a_day_with_three_sessions_auto_lists_both(self):
        need = ds(EARLY, LATE, EVENING, per_day=2, choice_needed=True)
        proposal = propose(Snapshot((need,)))

        assert codes(proposal) == [IssueCode.GROUP_TIME_AUTO]
        assert "3 занятия в день (8:15, 11:15, 15:00)" in proposal.issues[0].message
        assert "(8:15, 11:15), выберите" in proposal.issues[0].message

    def test_no_warning_without_dates(self):
        proposal = propose(Snapshot((Need(7, 70, Kind.DS_GROUP, "ДС", (), (EARLY, LATE)),)))

        assert not proposal.issues


# --- Шахматка: возможные времена группы без выбора (reserved) ------------------------------

SLOTS = [GridSlot(1, 495, 525), GridSlot(2, 550, 580), GridSlot(3, 675, 705)]
COLUMN = BoardColumn("10", (Member(10, True, frozenset({1, 2, 3})),))


def plan(patient: Patient) -> tuple[Seat, ...]:
    return plan_day([patient], [COLUMN], SLOTS)[0].seats


class TestReserved:
    def test_auto_avoids_reserved(self):
        patient = Patient(1, 1, busy=((495, 525),), reserved=((495, 525), (550, 580)))

        assert plan(patient) == (Seat(10, 3),)

    def test_auto_places_anyway_when_nothing_else(self):
        patient = Patient(1, 1, busy=((675, 705),), reserved=((495, 525), (550, 580)))

        assert plan(patient) == (Seat(10, 1),)

    def test_carry_ignores_reserved(self):
        patient = Patient(1, 1, seats=(Seat(10, 2),), reserved=((550, 580),))

        assert plan(patient) == (Seat(10, 2),)
