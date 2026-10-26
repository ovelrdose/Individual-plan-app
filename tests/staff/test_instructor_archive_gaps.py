"""Архив инструктора — сценарии, которых нет в test_instructor_archive*.py (TZ.md FR-STF-1,
решение 83): возврат в тот же день, экраны «Смены»/«Распорядок» после архива, пациенты чужих
отделений в итоге, удаление неработавшего с автором в журнале. Сегодня — вторник 06.10.2026.
Пара 2/2 Волков (05–06, 09–10) / Лебедева (07–08), Соколов — 5/2. Данные — вымышленные."""

from datetime import date

import pytest
from django.urls import reverse

from apps.accounts.models import Preset
from apps.programs.models import Prescription, Program
from apps.programs.services import add_prescription, save_program
from apps.scheduling import board, board_days, staff_changes
from apps.scheduling.models import BoardPatient, BookingKind
from apps.staff import services as staff_services
from apps.staff.models import Instructor, ShiftException

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def column_labels(user, day: date) -> list[str]:
    return [column.label for column in board.board(user, day).columns]


class TestRestoreSameDay:
    def test_restored_today_not_working_today(self, rehab, staff):
        volkov = staff["volkov"]  # 06.10 — его рабочий день по 2/2
        staff_changes.delete_instructor(rehab, volkov)

        staff_changes.restore_instructor(rehab, volkov)

        calendar = staff_services.instructor_calendars([volkov], TUE, TUE)[volkov.pk]
        assert not calendar.is_working(TUE)

    def test_archived_today_not_working_today(self, rehab, staff):
        """«С сегодняшнего дня его нет в сменах и шахматке»."""
        volkov = staff["volkov"]
        board_days.ensure_boards()
        assert "Волков" in " ".join(column_labels(rehab, TUE))

        staff_changes.delete_instructor(rehab, volkov)

        assert "Волков" not in " ".join(column_labels(rehab, TUE))
        assert volkov.pk not in staff_services.instructor_calendars(None, TUE, TUE)


class TestScreensAfterArchive:
    def test_shifts_screen_hides_archived(self, client, rehab, staff):
        staff_changes.delete_instructor(rehab, staff["sokolov"])
        client.force_login(rehab)

        html = client.get(reverse("staff:shifts")).content.decode()

        assert "Волков" in html and "Соколов" not in html

    def test_duties_screen_hides_archived(self, client, rehab, staff):
        staff_changes.delete_instructor(rehab, staff["sokolov"])
        client.force_login(rehab)

        response = client.get(reverse("staff:duties"))

        assert response.status_code == 200
        assert "Соколов" not in response.content.decode()

    def test_catalog_lists_archived_folded(self, client, rehab, staff):
        staff_changes.delete_instructor(rehab, staff["sokolov"])
        client.force_login(rehab)

        html = client.get(reverse("catalog:index"), {"tab": "instructors"}).content.decode()

        folded = html[html.index("В архиве (1)") :]
        assert "Соколов" in folded


class TestUsed:
    def test_only_past_exception_makes_used(self, rehab, slots):
        new = Instructor.objects.create(full_name="Новиков Н.Н.", short_name="Новиков")
        ShiftException.objects.create(instructor=new, date=MON, is_working=True)

        removed = staff_changes.delete_instructor(rehab, new)

        new.refresh_from_db()
        assert removed.archived and not new.is_active
        assert ShiftException.objects.filter(instructor=new, date=MON).exists()

    def test_unused_delete_logged_with_author(self, client, rehab, slots):
        new = Instructor.objects.create(full_name="Новиков Н.Н.", short_name="Новиков")
        client.force_login(rehab)
        url = reverse("catalog:instructor_delete", args=[new.pk])

        step = htmx(client, url)
        assert "удалится совсем" in step and "Пара 2/2" not in step
        html = htmx(client, url, {"confirm": "1"})

        assert "Инструктор «Новиков» удалён" in html
        last = Instructor.history.filter(id=new.pk).order_by("-history_date").first()
        assert last.history_type == "-" and last.history_user == rehab
        table = html[html.index('id="instructors-table"') :]
        assert "Новиков" not in table


class TestOtherDepartmentPatients:
    def test_other_department_patient_counted_not_named(
        self, client, rehab, admin_user, make_user, other_department, procedures, staff, slots
    ):
        """В итоге архива фамилии — только своих отделений (FR-ACC-2)."""
        stranger = make_user("stranger", (other_department, Preset.DOCTOR), short_name="Д")
        program = save_program(
            stranger,
            Program(
                department=other_department,
                full_name="Чужинов Тест Тестович",
                history_number="1",
                room="3",
                shrm=4,
                attending_doctor=stranger,
                start_date=MON,
            ),
            end_date_changed=False,
        )
        add_prescription(
            stranger, Prescription(program=program, procedure=procedures["individual"])
        )
        board_days.ensure_boards()
        for booking in program.bookings.filter(kind=BookingKind.INDIVIDUAL, date=TUE):
            board.remove_patient(admin_user, booking, booking.version)
        board.place_patient(admin_user, program, staff["volkov"], slots["9:10"], TUE)
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("catalog:instructor_delete", args=[staff["volkov"].pk]),
            {"confirm": "1"},
        )

        assert BoardPatient.objects.get(program=program, date=TUE).unplaced == 1
        assert "Чужинов" not in html
        assert "пациентов других отделений: 1" in html
