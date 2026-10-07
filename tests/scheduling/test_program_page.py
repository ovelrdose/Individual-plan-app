"""Страница программы и карта с индивидуальным из шахматки (TZ.md FR-SCH-16, FR-CRD-2) и то,
что убрано из прототипа (§7.5).

Сегодня — вторник 06.10.2026, следующий будний — среда 07.10. Инструкторы вымышленные: пара
2/2 Волков (работает 05–06) / Лебедева (07–08), Соколов — 5/2.
"""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.cards.services import build_card_data
from apps.scheduling import board, board_days, manual, staff_changes
from apps.scheduling.models import Booking, BookingKind
from apps.scheduling.services import program_schedule
from tests.scheduling.test_board_days import TUE, WED


@pytest.fixture(autouse=True)
def today(monkeypatch):
    current = {"day": TUE}
    monkeypatch.setattr(board_days, "today", lambda: current["day"])
    return current


def typical_individual(program) -> list[tuple[time, str]]:
    return [
        (time(row.start // 60, row.start % 60), row.who)
        for row in program_schedule(program).typical
        if row.label == "Инд.занятие"
    ]


class TestTypicalDay:
    def test_today_board_wins(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient()  # сегодня — «Не распределены», завтра — автоматически
        board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)

        assert typical_individual(program) == [(time(9, 50), "Соколов")]

    def test_tomorrow_when_not_in_today_grid(self, patient):
        board_days.ensure_boards()
        program = patient()
        tomorrow = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)

        assert typical_individual(program) == [(tomorrow.start, tomorrow.instructor.team_label)]

    def test_past_boards_do_not_count(self, patient, rehab, staff, slots, today):
        board_days.ensure_boards()
        program = patient()
        board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], TUE)
        today["day"] = WED
        board_days.ensure_boards()
        wed = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)

        # Вторник прошёл: в типичный день — среда, а не «самое частое» время.
        assert typical_individual(program) == [(wed.start, wed.instructor.team_label)]
        assert not any("Инд" in issue["message"] for issue in program_schedule(program).warnings)

    def test_card_uses_nearest_board(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient()
        board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)

        rows = [(item.start, item.label) for item in build_card_data(program).schedule]
        assert (time(9, 50), "Инд.занятие") in rows

    def test_no_board_no_row(self, patient, today):
        program = patient()  # шахматок ещё нет — но первая раскладывает всех (FR-SCH-13)
        Booking.objects.filter(program=program, kind=BookingKind.INDIVIDUAL).delete()
        assert typical_individual(program) == []


class TestCalendarAndBoards:
    def test_board_lines_show_hold(self, patient):
        board_days.ensure_boards()
        program = patient()

        lines = {line.date: line for line in program_schedule(program).boards}
        assert lines[TUE].text == "в блоке «Не распределены» (1)" and lines[TUE].hold
        assert lines[WED].text.endswith("Лебедева") and not lines[WED].hold

    def test_calendar_cells(self, patient):
        board_days.ensure_boards()
        program = patient()
        schedule = program_schedule(program)
        column = next(i for i, c in enumerate(schedule.columns) if c.procedure.kind == "INDIVIDUAL")
        cells = {row.date: row.cells[column] for row in schedule.calendar}

        assert cells[TUE].hold == "не распределён" and cells[TUE].individual
        assert cells[WED].bookings and not cells[WED].pending
        assert cells[date(2026, 10, 8)].pending  # шахматки на четверг ещё нет
        assert not cells[date(2026, 10, 10)].active  # суббота — шахматки нет

    def test_program_page(self, client, patient, rehab):
        board_days.ensure_boards()
        program = patient()
        client.force_login(rehab)
        page = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "Шахматка Вт 06.10" in page and "Не распределены" in page
        assert "Подобрать заново" not in page and "Перестройки" not in page
        individual = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        # Время индивидуального ведёт в шахматку, а не в панель правки.
        assert reverse("scheduling:booking_panel", args=[individual.pk]) not in page
        assert f"{reverse('scheduling:board')}?date=2026-10-07" in page


class TestRemovedFromPrototype:
    def test_individual_is_not_edited_on_program_page(self, client, patient, rehab):
        board_days.ensure_boards()
        program = patient()
        booking = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        client.force_login(rehab)

        assert client.get(reverse("scheduling:booking_panel", args=[booking.pk])).status_code == 404
        with pytest.raises(manual.EditError, match="в шахматке"):
            manual.remove_booking(rehab, booking, booking.version)
        with pytest.raises(manual.EditError, match="в шахматке"):
            manual.unpin(rehab, booking, booking.version)
        with pytest.raises(manual.EditError, match="в шахматке"):
            manual.edit_booking(rehab, booking, booking.version, manual.Target())

    def test_not_working_sends_to_unplaced_without_summary(
        self,
        client,
        patient,
        rehab,
        staff,
        slots,
    ):
        board_days.ensure_boards()
        program = patient()
        board.place_patient(rehab, program, staff["sokolov"], slots["10:30"], TUE)
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:board_off"),
            {"instructor": staff["sokolov"].pk, "date": "2026-10-06"},
        )

        assert response.url == f"{reverse('scheduling:board')}?date=2026-10-06"
        assert not Booking.objects.filter(
            program=program, date=TUE, kind=BookingKind.INDIVIDUAL
        ).exists()
        assert staff_changes.rebuild(staff["sokolov"], TUE, TUE) is None
