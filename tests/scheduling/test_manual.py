"""Ручная правка на странице программы (TZ.md FR-SCH-9/10, решение 57).

Справочник — полный, инструкторы — вымышленные из seed_dev (см. test_board.py).
"""

from datetime import date, time, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db.models import Count
from django.urls import reverse

from apps.accounts.models import Role
from apps.catalog.models import GroupSession, InstructorSlot, Procedure
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling import board, manual
from apps.scheduling.domain.validate import ViolationCode
from apps.scheduling.models import Booking, BookingSource, RemovedSession
from apps.scheduling.services import program_schedule, replan
from apps.staff.models import Instructor

pytestmark = pytest.mark.usefixtures("full_catalog")

MONDAY = date(2026, 10, 5)
SATURDAY = date(2026, 10, 10)
INDIVIDUAL = "Индивидуальное занятие"


@pytest.fixture
def staff(settings, doctor, rehab, admin_user) -> dict[str, Instructor]:
    settings.DEBUG = True
    call_command("seed_dev", stdout=StringIO())
    return {item.short_name: item for item in Instructor.objects.all()}


def prescribe(user, program: Program, name: str, **fields) -> Prescription:
    procedure = Procedure.objects.get(name=name, department=None)
    return programs.add_prescription(
        user, Prescription(program=program, procedure=procedure, **fields)
    )


@pytest.fixture
def patient(doctor, staff, make_program) -> Program:
    program = make_program(full_name="Иванов Иван Иванович", room="9")
    prescribe(doctor, program, INDIVIDUAL)
    prescribe(doctor, program, "Эрго общая")
    prescribe(doctor, program, "st-150")
    return program


def get(program: Program, kind: str, day: date = MONDAY) -> Booking:
    return program.bookings.get(kind=kind, date=day)


def weekday_seat(day: date, *, exclude: int | None = None, after: time = time(12)):
    """Свободная дневная ячейка после обеда (подальше от занятий пациента)."""
    for instructor, slot in board.free_seats(day):
        if slot.is_evening or slot.start < after or instructor.pk == exclude:
            continue
        return instructor, slot
    raise AssertionError("нет свободной ячейки")


class TestIndividual:
    def test_this_date_only(self, rehab, patient):
        booking = get(patient, "INDIVIDUAL")
        instructor, slot = weekday_seat(MONDAY, exclude=booking.instructor_id, after=time(15))
        tuesday_before = get(patient, "INDIVIDUAL", MONDAY + timedelta(days=1))

        result = manual.edit_booking(
            rehab, booking, booking.version, manual.Target(instructor=instructor, slot=slot)
        )

        assert result.changed == [MONDAY]
        moved = get(patient, "INDIVIDUAL")
        assert (moved.instructor, moved.slot, moved.start) == (instructor, slot, slot.start)
        assert moved.pinned and moved.source == BookingSource.MANUAL
        tuesday = get(patient, "INDIVIDUAL", MONDAY + timedelta(days=1))
        assert (tuesday.instructor_id, tuesday.start) == (
            tuesday_before.instructor_id,
            tuesday_before.start,
        )

    def test_this_and_following_keeps_team_and_skips_impossible(self, rehab, patient, staff):
        booking = get(patient, "INDIVIDUAL")
        # Пара 2/2: в даты, когда Волков не работает, ставится Лебедева.
        pair = staff["Волков"]
        slot = InstructorSlot.objects.get(start=time(14, 20))

        result = manual.edit_booking(
            rehab,
            booking,
            booking.version,
            manual.Target(instructor=pair, slot=slot),
            scope=manual.FOLLOWING,
        )

        assert MONDAY in result.changed
        weekdays = patient.bookings.filter(kind="INDIVIDUAL", instructor__isnull=False)
        assert {b.instructor.short_name for b in weekdays} <= {"Волков", "Лебедева"}
        assert {b.start for b in weekdays} == {time(14, 20)}
        assert all(b.pinned for b in weekdays)
        weekend = patient.bookings.filter(kind="INDIVIDUAL", date=SATURDAY).get()
        assert weekend.instructor is None and weekend.start == time(14, 20)

    def test_overlap_on_this_date_is_refused(self, rehab, patient):
        booking = get(patient, "INDIVIDUAL")
        group = get(patient, "LFK_GROUP")
        slot = InstructorSlot.objects.get(start=group.start)
        instructor = next(i for i, s in board.free_seats(MONDAY) if s == slot)

        with pytest.raises(manual.EditError) as error:
            manual.edit_booking(
                rehab, booking, booking.version, manual.Target(instructor=instructor, slot=slot)
            )

        assert error.value.violations[0].code == ViolationCode.PATIENT_OVERLAP
        assert not get(patient, "INDIVIDUAL").pinned

    def test_weekend_changes_time_only(self, rehab, patient):
        saturday = get(patient, "INDIVIDUAL", SATURDAY)
        slot = InstructorSlot.objects.get(start=time(16, 20))

        manual.edit_booking(rehab, saturday, saturday.version, manual.Target(slot=slot))

        saturday = get(patient, "INDIVIDUAL", SATURDAY)
        assert saturday.instructor is None and saturday.start == time(16, 20)
        assert saturday.pinned

    def test_stale_version(self, rehab, patient):
        booking = get(patient, "INDIVIDUAL")

        with pytest.raises(manual.EditError, match="изменена другим пользователем"):
            manual.edit_booking(rehab, booking, booking.version + 1, manual.Target())


class TestGroupAndEquipment:
    def test_group_moves_to_other_session(self, doctor, rehab, patient):
        group = (
            Procedure.objects.filter(kind="LFK_GROUP", department=None, sessions__is_active=True)
            .annotate(n=Count("sessions"))
            .filter(n__gte=2)
            .order_by("name")
            .first()
        )
        prescribe(doctor, patient, group.name)
        booking = patient.bookings.get(procedure=group, date=MONDAY)
        other = (
            GroupSession.objects.filter(procedure=group, is_active=True)
            .exclude(pk=booking.group_session_id)
            .first()
        )
        # Индивидуальное в это время подбор при пересборке переставит.
        busy = patient.bookings.filter(
            date=MONDAY, start__lt=other.end_time, end__gt=other.start_time
        )
        if busy.exclude(kind="INDIVIDUAL").exists():
            pytest.skip("второе занятие группы пересекается с другой группой пациента")
        busy.filter(kind="INDIVIDUAL").delete()

        result = manual.edit_booking(rehab, booking, booking.version, manual.Target(session=other))

        assert result.changed == [MONDAY]
        moved = patient.bookings.get(procedure=group, date=MONDAY)
        assert moved.group_session == other and moved.start == other.start_time
        assert moved.pinned

    def test_session_of_other_group_is_refused(self, rehab, patient):
        booking = get(patient, "LFK_GROUP")
        foreign = GroupSession.objects.exclude(procedure=booking.procedure).first()

        with pytest.raises(manual.EditError, match="время группы"):
            manual.edit_booking(rehab, booking, booking.version, manual.Target(session=foreign))

    def test_equipment_off_window_needs_reason(self, rehab, patient):
        booking = get(patient, "EQUIPMENT")

        with pytest.raises(manual.ConfirmationRequired) as error:
            manual.edit_booking(rehab, booking, booking.version, manual.Target(start=time(16, 50)))
        assert error.value.violations[0].code == ViolationCode.EQUIPMENT_WINDOW

        manual.edit_booking(
            rehab, booking, booking.version, manual.Target(start=time(16, 50)), reason="вечером"
        )
        assert get(patient, "EQUIPMENT").start == time(16, 50)

    def test_equipment_full_needs_note_and_keeps_it(
        self, doctor, rehab, patient, make_program, staff
    ):
        booking = get(patient, "EQUIPMENT")
        start = time(14)
        capacity = booking.equipment.capacity
        for i in range(capacity):
            other = make_program(full_name=f"Тренажёров{i} Тимур Тимурович", room=str(20 + i))
            prescribe(doctor, other, "st-150")
            item = get(other, "EQUIPMENT")
            manual.edit_booking(rehab, item, item.version, manual.Target(start=start), reason="x")
        booking.refresh_from_db()

        with pytest.raises(manual.ConfirmationRequired) as error:
            manual.edit_booking(rehab, booking, booking.version, manual.Target(start=start))
        assert ViolationCode.EQUIPMENT_FULL in [v.code for v in error.value.violations]

        manual.edit_booking(
            rehab, booking, booking.version, manual.Target(start=start), reason="сверх"
        )
        assert get(patient, "EQUIPMENT").note == "сверх"


class TestRemoveRestoreUnpin:
    def test_remove_following_and_restore_one(self, rehab, patient):
        booking = get(patient, "LFK_GROUP", MONDAY + timedelta(days=10))

        result = manual.remove_booking(rehab, booking, booking.version, scope=manual.FOLLOWING)

        assert result.changed == [MONDAY + timedelta(days=d) for d in range(10, 15)]
        assert not patient.bookings.filter(kind="LFK_GROUP", date__gte=booking.date).exists()
        assert RemovedSession.objects.filter(prescription=booking.prescription).count() == 5
        cell = next(
            c
            for row in program_schedule(patient).calendar
            if row.date == booking.date
            for c in row.cells
            if c.removed
        )
        assert cell.removed == 1

        manual.restore_date(rehab, booking.prescription, booking.date)

        assert patient.bookings.filter(kind="LFK_GROUP", date=booking.date).exists()
        with pytest.raises(manual.EditError, match="не убирали"):
            manual.restore_date(rehab, booking.prescription, booking.date)

    def test_unpin_following_returns_control_to_planner(self, rehab, patient):
        booking = get(patient, "INDIVIDUAL")
        instructor, slot = weekday_seat(MONDAY, exclude=booking.instructor_id, after=time(15))
        manual.edit_booking(
            rehab,
            booking,
            booking.version,
            manual.Target(instructor=instructor, slot=slot),
            scope=manual.FOLLOWING,
        )
        assert patient.bookings.filter(kind="INDIVIDUAL", pinned=True).exists()
        booking = get(patient, "INDIVIDUAL")

        result = manual.unpin(rehab, booking, booking.version, scope=manual.FOLLOWING)

        assert result.changed
        assert not patient.bookings.filter(kind="INDIVIDUAL", pinned=True).exists()
        replan(patient)


class TestRights:
    def test_doctor_cannot_edit(self, doctor, patient):
        booking = get(patient, "INDIVIDUAL")

        with pytest.raises(manual.EditError, match="специалист ФР"):
            manual.remove_booking(doctor, booking, booking.version)

    def test_rehab_of_other_department_cannot_edit(self, make_user, other_department, patient):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        booking = get(patient, "INDIVIDUAL")

        with pytest.raises(manual.EditError):
            manual.unpin(stranger, booking, booking.version)


class TestViews:
    def test_calendar_has_edit_buttons_for_rehab_only(self, client, doctor, rehab, patient):
        booking = get(patient, "INDIVIDUAL")
        url = reverse("programs:detail", args=[patient.pk])
        panel = reverse("scheduling:booking_panel", args=[booking.pk])

        client.force_login(doctor)
        assert panel not in client.get(url).content.decode()
        assert client.get(panel).status_code == 403
        client.force_login(rehab)
        assert panel in client.get(url).content.decode()

    @pytest.mark.parametrize("kind", ["INDIVIDUAL", "LFK_GROUP", "EQUIPMENT"])
    def test_panel_renders_for_each_kind(self, client, rehab, patient, kind):
        client.force_login(rehab)
        booking = get(patient, kind)

        html = client.get(reverse("scheduling:booking_panel", args=[booking.pk])).content.decode()

        assert "эта и все следующие" in html and "Убрать" in html

    def test_weekend_panel_offers_time_only(self, client, rehab, patient):
        client.force_login(rehab)
        booking = get(patient, "INDIVIDUAL", SATURDAY)

        html = client.get(reverse("scheduling:booking_panel", args=[booking.pk])).content.decode()

        assert "в выходной — без инструктора" in html

    def test_edit_view_returns_panel_and_schedule(self, client, rehab, patient):
        client.force_login(rehab)
        booking = get(patient, "EQUIPMENT")

        response = client.post(
            reverse("scheduling:booking_edit", args=[booking.pk]),
            {"version": booking.version, "start": "14:00", "scope": "one"},
            headers={"HX-Request": "true"},
        )

        html = response.content.decode()
        assert "Занятие изменено и закреплено." in html
        assert 'id="schedule" hx-swap-oob="true"' in html

    def test_confirmation_in_panel(self, client, rehab, patient):
        client.force_login(rehab)
        booking = get(patient, "EQUIPMENT")

        html = client.post(
            reverse("scheduling:booking_edit", args=[booking.pk]),
            {"version": booking.version, "start": "16:50"},
        ).content.decode()

        assert 'name="reason"' in html and "вне окна" in html

    def test_remove_and_restore_views(self, client, rehab, patient):
        client.force_login(rehab)
        booking = get(patient, "LFK_GROUP")

        html = client.post(
            reverse("scheduling:booking_remove", args=[booking.pk]),
            {"version": booking.version},
        ).content.decode()
        assert "подбор его не вернёт" in html and "убрано" in html

        html = client.post(
            reverse("scheduling:booking_restore", args=[patient.pk, booking.prescription_id]),
            {"date": "2026-10-05"},
        ).content.decode()
        assert "Занятие возвращено" in html

    def test_other_department_booking_is_404(self, client, make_user, other_department, patient):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        client.force_login(stranger)

        booking = get(patient, "INDIVIDUAL")
        assert client.get(reverse("scheduling:booking_panel", args=[booking.pk])).status_code == 404

    @pytest.mark.parametrize("value", ["", "²", "abc:1", "1:x"])
    def test_garbage_target_is_404(self, client, rehab, patient, value):
        client.force_login(rehab)
        booking = get(patient, "INDIVIDUAL")

        response = client.post(
            reverse("scheduling:booking_edit", args=[booking.pk]),
            {"version": booking.version, "target": value},
        )

        assert response.status_code == 404


class TestPreferFromBoard:
    def test_prefer_instructor_from_cell(self, rehab, patient):
        booking = get(patient, "INDIVIDUAL")
        instructor = booking.instructor

        board.prefer_instructor(rehab, booking)

        patient.refresh_from_db()
        assert patient.preferred_instructor == instructor
