"""Индивидуальные занятия в расписании программы (шаг 3.2) на настоящей БД.

Инструкторы вымышленные: Волков и Лебедева — пара 2/2, Соколов — каждый день.
"""

import time as clock
from datetime import date, time, timedelta
from io import BytesIO

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Role
from apps.catalog.models import InstructorSlot, Procedure
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling import services
from apps.scheduling.models import Booking, BookingSource
from apps.scheduling.services import program_schedule, replan
from apps.staff.models import Instructor, InstructorDuty, ShiftPattern
from apps.staff.services import set_partner

pytestmark = pytest.mark.usefixtures("full_catalog")

START = date(2026, 10, 5)  # курс ШРМ 4: 05.10–19.10


def proc(name: str) -> Procedure:
    return Procedure.objects.get(name=name, department=None)


def prescribe(doctor, program, name, **fields) -> Prescription:
    return programs.add_prescription(
        doctor, Prescription(program=program, procedure=proc(name), **fields)
    )


def individuals(program: Program):
    # Будни: в выходные инструктора нет (решение 56).
    return program.bookings.filter(kind="INDIVIDUAL", instructor__isnull=False).select_related(
        "instructor", "slot"
    )


def seats(program: Program) -> set[tuple[str, time]]:
    return {(b.instructor.short_name, b.start) for b in individuals(program)}


@pytest.fixture
def team(db) -> dict[str, Instructor]:
    def make(name: str, order: int, pattern: str, anchor: date) -> Instructor:
        item = Instructor.objects.create(short_name=name, full_name=name, display_order=order)
        ShiftPattern.objects.create(
            instructor=item, pattern=pattern, anchor_date=anchor, valid_from=date(2026, 10, 1)
        )
        return item

    items = {
        "Волков": make("Волков", 10, "2/2", START),
        "Лебедева": make("Лебедева", 40, "2/2", START + timedelta(days=2)),
        "Соколов": make("Соколов", 30, "daily", START),
    }
    set_partner(items["Волков"], items["Лебедева"])
    return items


@pytest.fixture
def program(make_program):
    return make_program(shrm=4)


class TestReplan:
    def test_individual_is_placed_with_instructor_and_slot(self, doctor, team, program):
        prescribe(doctor, program, "Эрго общая")  # 9:10
        prescribe(doctor, program, "Индивидуальное занятие")

        bookings = list(individuals(program))
        assert len(bookings) == 11, "инструктор — только по будням (решение 56)"
        weekend = program.bookings.filter(kind="INDIVIDUAL", instructor__isnull=True)
        assert {b.date.weekday() for b in weekend} == {5, 6} and weekend.count() == 4
        assert {b.start for b in weekend} == {time(9, 50)}
        # 9:10 занят группой — 9:50, у пары (раньше в шахматке, загрузка равная).
        assert {(b.start, b.end) for b in bookings} == {(time(9, 50), time(10, 20))}
        assert {b.slot.start for b in bookings} == {time(9, 50)}
        for b in bookings:
            assert b.source == BookingSource.AUTO and not b.pinned
            expected = "Волков" if (b.date - START).days % 4 in (0, 1) else "Лебедева"
            assert b.instructor.short_name == expected
        program.refresh_from_db()
        assert program.schedule_issues == []

    def test_without_instructors_is_a_conflict(self, doctor, program):
        prescribe(doctor, program, "Индивидуальное занятие")

        program.refresh_from_db()
        assert not program.bookings.exists()
        assert program.has_schedule_conflicts
        assert "нет свободного инструктора" in program.schedule_issues[0]["message"]

    def test_duty_slot_is_not_used(self, doctor, team, program):
        Instructor.objects.filter(short_name__in=["Волков", "Лебедева"]).update(is_active=False)
        InstructorDuty.objects.create(
            instructor=team["Соколов"],
            slot=InstructorSlot.objects.get(start=time(9, 10)),
            kind="METHOD_WORK",
            valid_from=date(2026, 10, 1),
        )

        prescribe(doctor, program, "Индивидуальное занятие")

        assert seats(program) == {("Соколов", time(9, 50))}

    def test_other_program_of_other_department_takes_the_slot(
        self, doctor, make_user, other_department, team, program
    ):
        Instructor.objects.filter(short_name="Соколов").update(is_active=False)
        other_doctor = make_user("other", (other_department, Role.DOCTOR))
        other = programs.save_program(
            other_doctor,
            Program(
                department=other_department,
                full_name="Другой Д.Д.",
                room="3",
                shrm=4,
                attending_doctor=other_doctor,
                start_date=START,
            ),
            end_date_changed=False,
        )
        prescribe(other_doctor, other, "Индивидуальное занятие")
        assert seats(other) == {("Волков", time(9, 10)), ("Лебедева", time(9, 10))}

        prescribe(doctor, program, "Индивидуальное занятие")

        assert {start for _, start in seats(program)} == {time(9, 50)}

    def test_stable_when_prescription_added(self, doctor, rehab, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")
        # Прошлая автоматическая постановка — у Соколова в 13:00 (например, до правки смен).
        slot = InstructorSlot.objects.get(start=time(13, 0))
        individuals(program).update(
            instructor=team["Соколов"], slot=slot, start=time(13, 0), end=time(13, 30)
        )

        prescribe(doctor, program, "Эрго общая")

        assert seats(program) == {("Соколов", time(13, 0))}, "пациент не «прыгает» (решение 49)"

        services.replan_by(rehab, program)

        assert seats(program) == {("Волков", time(9, 50)), ("Лебедева", time(9, 50))}

    def test_pinned_individual_counts_and_is_kept(self, doctor, team, program):
        item = prescribe(doctor, program, "Индивидуальное занятие", per_day=2)
        first = individuals(program).order_by("date", "start").first()
        first.pinned = True
        first.save()

        replan(program)

        assert Booking.objects.filter(pk=first.pk).exists()
        assert item.bookings.filter(date=first.date).count() == 2
        assert item.bookings.count() == 30

    def test_list_counts_individual_as_scheduled(self, client, doctor, team, make_program):
        current = make_program(start_date=date.today())
        prescribe(doctor, current, "Индивидуальное занятие")
        client.force_login(doctor)

        html = client.get(reverse("programs:list")).content.decode()

        assert "расписание составлено" in html


class TestDatabaseGuard:
    def booking(self, program, prescription, **fields) -> Booking:
        slot = InstructorSlot.objects.get(start=time(9, 10))
        values = {
            "program": program,
            "prescription": prescription,
            "procedure": prescription.procedure,
            "kind": "INDIVIDUAL",
            "date": START,
            "start": time(9, 10),
            "end": time(9, 40),
            "slot": slot,
        } | fields
        return Booking.objects.create(**values)

    def test_instructor_slot_is_unique(self, doctor, team, make_program):
        first = make_program(full_name="Первый П.П.")
        second = make_program(full_name="Второй В.В.")
        a = Prescription.objects.create(program=first, procedure=proc("Индивидуальное занятие"))
        b = Prescription.objects.create(program=second, procedure=proc("Индивидуальное занятие"))
        self.booking(first, a, instructor=team["Соколов"])

        with pytest.raises(IntegrityError), transaction.atomic():
            self.booking(second, b, instructor=team["Соколов"])

    def test_individual_requires_instructor_and_slot(self, doctor, team, program):
        item = Prescription.objects.create(
            program=program, procedure=proc("Индивидуальное занятие")
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            self.booking(program, item, instructor=None)

    def test_group_has_no_instructor(self, doctor, team, program):
        item = Prescription.objects.create(program=program, procedure=proc("Эрго общая"))

        with pytest.raises(IntegrityError), transaction.atomic():
            self.booking(program, item, kind="LFK_GROUP", instructor=team["Соколов"])


class TestPreferredInstructor:
    def test_rehab_chooses_and_schedule_follows(self, doctor, rehab, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")

        services.choose_preferred_instructor(rehab, program, team["Соколов"])

        program.refresh_from_db()
        assert program.preferred_instructor == team["Соколов"]
        assert seats(program) == {("Соколов", time(9, 10))}
        change = program.history.filter(preferred_instructor=team["Соколов"]).last()
        assert change.history_user == rehab, "автор выбора — в журнале (NFR-5)"

        services.choose_preferred_instructor(rehab, program, None)
        program.refresh_from_db()
        assert program.preferred_instructor is None

    def test_pair_member_replaced_by_partner_with_warning(self, doctor, rehab, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")

        services.choose_preferred_instructor(rehab, program, team["Волков"])

        program.refresh_from_db()
        for b in individuals(program):
            expected = "Волков" if (b.date - START).days % 4 in (0, 1) else "Лебедева"
            assert b.instructor.short_name == expected
        assert [i["code"] for i in program.schedule_issues] == ["PREFERRED_REPLACED"]

    def test_doctor_cannot_choose(self, doctor, team, program):
        with pytest.raises(PermissionDenied):
            services.choose_preferred_instructor(doctor, program, team["Соколов"])

    def test_inactive_instructor_is_rejected(self, rehab, team, program):
        Instructor.objects.filter(pk=team["Соколов"].pk).update(is_active=False)

        with pytest.raises(services.ScheduleError):
            services.choose_preferred_instructor(rehab, program, team["Соколов"])

    def test_view_htmx(self, client, doctor, rehab, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")
        url = reverse("scheduling:program_preferred_instructor", args=[program.pk])
        client.force_login(rehab)

        response = client.post(
            url, {"instructor": team["Соколов"].pk}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="schedule"' in html
        assert f'<option value="{team["Соколов"].pk}" selected>Соколов</option>' in html
        assert "9:10 Соколов" in html, "календарь показывает инструктора"

    def test_view_without_htmx_redirects(self, client, doctor, rehab, team, program):
        url = reverse("scheduling:program_preferred_instructor", args=[program.pk])
        client.force_login(rehab)

        response = client.post(url, {"instructor": ""})

        assert response.url == reverse("programs:detail", args=[program.pk])

    def test_view_rights(self, client, doctor, make_user, other_department, team, program):
        url = reverse("scheduling:program_preferred_instructor", args=[program.pk])

        client.force_login(doctor)
        assert client.post(url, {"instructor": team["Соколов"].pk}).status_code == 403

        client.force_login(make_user("stranger", (other_department, Role.REHAB)))
        assert client.post(url, {"instructor": team["Соколов"].pk}).status_code == 404

    def test_doctor_sees_value_without_form(self, client, doctor, rehab, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")
        services.choose_preferred_instructor(rehab, program, team["Соколов"])
        client.force_login(doctor)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "Инструктор по желанию пациента" in html
        assert 'name="instructor"' not in html


class TestScreenAndCard:
    def test_typical_day_shows_team(self, doctor, team, program):
        prescribe(doctor, program, "Индивидуальное занятие")

        schedule = program_schedule(program)

        row = next(r for r in schedule.day if not r.is_window)
        assert (row.start, row.label, row.who) == (550, "Инд.занятие", "Волков/ Лебедева")
        assert row.deviations == 0
        assert schedule.individual and schedule.instructors

    def test_card_has_individual_row(self, client, doctor, team, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "Индивидуальное занятие")
        client.force_login(doctor)

        sheet = load_workbook(
            BytesIO(client.get(reverse("cards:download", args=[program.pk])).content)
        ).active

        assert (sheet["B4"].value, sheet["C4"].value) == (time(9, 10), "Группа Эрго общая")
        # Индивидуальное занимает свою строку сетки — окно 9:50 перестаёт быть окном.
        assert sheet["B5"].value == time(9, 50)
        assert sheet["B5"].number_format == "h:mm"
        assert sheet["C5"].value == "Инд.занятие"
        assert sheet["B6"].value == time(10, 30) and sheet["C6"].value is None, "окно"


def test_replan_is_fast_with_many_programs(doctor, department, team, program):
    """NFR-1: подбор одной программы — до 2 секунд при ~150 активных программах.

    Грубый замер: 20 инструкторов каждый день, 150 программ с индивидуальным занятием на
    весь курс уже стоят в расписании (по одному в день, разные инструкторы и слоты).
    """
    extra = [
        Instructor(short_name=f"Инструктор{i}", full_name=f"Инструктор{i}", display_order=100 + i)
        for i in range(20)
    ]
    Instructor.objects.bulk_create(extra)
    ShiftPattern.objects.bulk_create(
        ShiftPattern(instructor=item, pattern="daily", anchor_date=START, valid_from=START)
        for item in extra
    )
    slots = list(InstructorSlot.objects.filter(is_evening=False))
    others = Program.objects.bulk_create(
        Program(
            department=department,
            full_name=f"Пациент{i}",
            room=str(i),
            shrm=4,
            attending_doctor=doctor,
            start_date=START,
            end_date=START + timedelta(days=14),
        )
        for i in range(150)
    )
    individual = proc("Индивидуальное занятие")
    prescriptions = Prescription.objects.bulk_create(
        Prescription(program=item, procedure=individual) for item in others
    )
    Booking.objects.bulk_create(
        Booking(
            program=item,
            prescription=prescription,
            procedure=individual,
            kind="INDIVIDUAL",
            date=START + timedelta(days=day),
            start=slots[i % len(slots)].start,
            end=slots[i % len(slots)].end,
            slot=slots[i % len(slots)],
            instructor=extra[i // len(slots) % len(extra)],
        )
        for i, (item, prescription) in enumerate(zip(others, prescriptions, strict=True))
        for day in range(15)
    )
    prescribe(doctor, program, "Эрго общая")
    prescribe(doctor, program, "st-150")
    prescribe(doctor, program, "Индивидуальное занятие", per_day=2)

    started = clock.perf_counter()
    replan(program)
    elapsed = clock.perf_counter() - started

    assert individuals(program).count() == 22
    assert elapsed < 2, f"подбор занял {elapsed:.2f} с"


@pytest.mark.django_db(transaction=True)
def test_parallel_replans_do_not_collide(doctor, team, make_program):
    """Два подбора разных программ одновременно: второй ждёт блокировки инструкторов и видит
    занятия первого — без IntegrityError на уникальном индексе (FR-SCH-2, FR-SCH-3)."""
    import threading

    from django.db import connection

    Instructor.objects.exclude(pk=team["Соколов"].pk).update(is_active=False)
    first = make_program(full_name="Первый П.П.")
    second = make_program(full_name="Второй В.В.")
    for item in (first, second):
        Prescription.objects.create(program=item, procedure=proc("Индивидуальное занятие"))

    locked, release = threading.Event(), threading.Event()
    errors: list[Exception] = []

    def hold_first() -> None:
        try:
            with transaction.atomic():
                replan(first)
                locked.set()
                release.wait(5)
        except Exception as exc:  # pragma: no cover - видно в errors
            errors.append(exc)
        finally:
            connection.close()

    def run_second() -> None:
        try:
            replan(second)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_first)
    holder.start()
    assert locked.wait(10)
    waiter = threading.Thread(target=run_second)
    waiter.start()
    waiter.join(0.5)
    assert waiter.is_alive(), "второй подбор ждёт блокировку инструкторов"
    release.set()
    holder.join(10)
    waiter.join(10)

    assert errors == []
    assert seats(first) == {("Соколов", time(9, 10))}
    assert seats(second) == {("Соколов", time(9, 50))}
