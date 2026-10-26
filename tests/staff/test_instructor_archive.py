"""Инструктор в архиве (TZ.md FR-STF-1, решение 83): уходит из смен и шахматки с сегодня, его
пациенты — в «Не распределены», пара 2/2 разрывается, распорядок заканчивается сегодня.

Сегодня — вторник 06.10.2026. Пара 2/2 Волков (05–06, 09–10) / Лебедева (07–08), Соколов — 5/2.
"""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.catalog.models import GroupSession
from apps.scheduling import board, board_days, staff_changes
from apps.scheduling.models import BoardPatient, Booking
from apps.staff import services as staff_services
from apps.staff.models import Instructor, InstructorBlock, InstructorDuty, ShiftPattern

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def test_archive_moves_patients_breaks_pair_and_ends_duties(
    client, rehab, patient, staff, slots, procedures
):
    board_days.ensure_boards()
    program = patient(start_date=MON)  # первая шахматка раскладывает и сегодня
    volkov = staff["volkov"]
    board.place_patient(rehab, program, volkov, slots["10:30"], TUE)
    led = GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))
    board.add_duty(rehab, volkov, slots["9:10"], MON, "GROUP_LEAD", group_session=led)
    board.add_block(rehab, volkov, slots["11:10"], WED, "BOS")
    client.force_login(rehab)
    url = reverse("catalog:instructor_delete", args=[volkov.pk])

    step = htmx(client, url)
    assert "уйдёт в архив" in step and "Лебедева останется один" in step
    html = htmx(client, url, {"confirm": "1"})

    volkov.refresh_from_db()
    staff["lebedeva"].refresh_from_db()
    assert not volkov.is_active and volkov.partner is None
    assert staff["lebedeva"].partner is None
    assert "Лебедева теперь работает один" in html
    assert "Не распределены" in html and "Пациентов9" in html
    assert not Booking.objects.filter(instructor=volkov, date__gte=TUE).exists()
    assert BoardPatient.objects.get(program=program, date=TUE).unplaced == 1
    duty = InstructorDuty.objects.get(instructor=volkov)
    assert duty.valid_to == TUE and duty.history.first().history_user == rehab
    assert ShiftPattern.objects.get(instructor=volkov).valid_to == TUE
    assert not InstructorBlock.objects.filter(instructor=volkov).exists()
    assert volkov not in staff_services.active_instructors()
    assert "В архиве (1)" in html


def test_restore_without_shift(rehab, staff):
    volkov = staff["volkov"]
    staff_changes.delete_instructor(rehab, volkov)

    staff_changes.restore_instructor(rehab, volkov)

    volkov.refresh_from_db()
    assert volkov.is_active
    calendar = staff_services.instructor_calendars([volkov], WED, WED)[volkov.pk]
    assert not calendar.is_working(WED)  # шаблон смены закончился — задают в «Сменах»


def test_unused_deleted(client, rehab, staff):
    new = Instructor.objects.create(full_name="Новиков Н.Н.", short_name="Новиков")
    staff_services.set_partner(new, staff["sokolov"])
    client.force_login(rehab)
    url = reverse("catalog:instructor_delete", args=[new.pk])

    assert "удалится совсем" in htmx(client, url)
    htmx(client, url, {"confirm": "1"})

    assert not Instructor.objects.filter(pk=new.pk).exists()
    staff["sokolov"].refresh_from_db()
    assert staff["sokolov"].partner is None


def test_shifts_link_to_catalog(client, rehab, staff):
    client.force_login(rehab)
    html = client.get(reverse("staff:shifts")).content.decode()
    assert reverse("catalog:index") + "?tab=instructors" in html
