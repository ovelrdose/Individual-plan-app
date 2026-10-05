"""Расписание программы на настоящей БД: подбор, ограничения, следование за изменениями."""

from datetime import date, time

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.catalog.models import Equipment, InstructorSlot, Procedure
from apps.programs import services as programs
from apps.programs.models import Prescription
from apps.scheduling.models import Booking
from apps.scheduling.services import program_schedule, replan
from apps.staff.models import Instructor

pytestmark = pytest.mark.usefixtures("full_catalog")


def proc(name):
    return Procedure.objects.get(name=name, department=None)


def prescribe(doctor, program, name, **fields):
    return programs.add_prescription(
        doctor, Prescription(program=program, procedure=proc(name), **fields)
    )


def starts(program, name):
    return {b.start for b in program.bookings.filter(procedure__name=name)}


@pytest.fixture
def program(make_program):
    return make_program(shrm=4)  # 05.10–19.10, 15 дней


class TestReplan:
    def test_groups_and_equipment_are_placed_on_add(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "I can нога")
        prescribe(doctor, program, "st-150")

        assert starts(program, "Эрго общая") == {time(9, 10)}
        assert starts(program, "I can нога") == {time(10, 30)}
        assert starts(program, "st-150") == {time(13, 0)}
        assert program.bookings.count() == 45
        program.refresh_from_db()
        assert program.schedule_issues == []

    def test_group_moves_to_free_session(self, doctor, program):
        prescribe(doctor, program, "I can нога")
        prescribe(doctor, program, "Нейро-тренинг")

        assert starts(program, "Нейро-тренинг") == {time(14, 20)}

    def test_card_only_is_not_scheduled(self, doctor, program):
        prescribe(doctor, program, "Алмаг")

        assert not program.bookings.exists()

    def test_pool_without_group_is_a_conflict(self, doctor, program):
        prescribe(doctor, program, "Бассейн")

        program.refresh_from_db()
        assert not program.bookings.exists()
        assert program.has_schedule_conflicts
        assert "группа бассейна" in program.schedule_issues[0]["message"]

    def test_pool_group(self, doctor, program):
        prescribe(doctor, program, "ЛФК в воде: нижняя конечность")

        assert starts(program, "ЛФК в воде: нижняя конечность") == {time(9, 0)}

    def test_prescription_dates(self, doctor, program):
        prescribe(
            doctor,
            program,
            "Эрго общая",
            start_date=date(2026, 10, 8),
            cancel_date=date(2026, 10, 12),
        )

        days = sorted(program.bookings.values_list("date", flat=True))
        assert days == [date(2026, 10, d) for d in (8, 9, 10, 11)]

    def test_cancel_and_delete_follow(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        item.cancel_date = date(2026, 10, 10)
        programs.update_prescription(doctor, item)
        assert program.bookings.count() == 5

        programs.delete_prescription(doctor, item)
        assert not program.bookings.exists()

    def test_course_change_follows(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        program.end_date = date(2026, 10, 9)

        programs.save_program(doctor, program, end_date_changed=True)

        assert sorted(program.bookings.values_list("date", flat=True))[-1] == date(2026, 10, 9)
        assert program.bookings.count() == 5

    def test_pinned_is_kept_unless_out_of_course(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        pinned = program.bookings.get(date=date(2026, 10, 6))
        pinned.pinned = True
        pinned.save()
        late = program.bookings.get(date=date(2026, 10, 18))
        late.pinned = True
        late.save()

        program.end_date = date(2026, 10, 15)
        programs.save_program(doctor, program, end_date_changed=True)

        assert Booking.objects.filter(pk=pinned.pk).exists()
        assert not Booking.objects.filter(pk=late.pk).exists()
        assert item.bookings.count() == 11

    def test_equipment_capacity_across_programs(self, doctor, make_program):
        Equipment.objects.filter(name="st-150").update(capacity=1)
        first = make_program(full_name="Первый П.П.", history_number="1")
        second = make_program(full_name="Второй В.В.", history_number="2")
        prescribe(doctor, first, "st-150")
        prescribe(doctor, second, "st-150")

        assert starts(first, "st-150") == {time(13, 0)}
        assert starts(second, "st-150") == {time(13, 15)}

    def test_replan_is_idempotent(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        before = list(program.bookings.values_list("date", "start"))

        replan(program)

        assert list(program.bookings.values_list("date", "start")) == before


class TestDatabaseGuard:
    def test_patient_cannot_be_in_two_places(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        other = prescribe(doctor, program, "I can нога")

        with pytest.raises(IntegrityError), transaction.atomic():
            Booking.objects.create(
                program=program,
                prescription=other,
                procedure=other.procedure,
                kind="LFK_GROUP",
                date=date(2026, 10, 5),
                start=time(9, 20),
                end=time(9, 50),
            )
        assert item.bookings.count() == 15

    def test_touching_intervals_are_allowed(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")  # 9:10–9:40

        Booking.objects.create(
            program=program,
            prescription=item,
            procedure=item.procedure,
            kind="LFK_GROUP",
            date=date(2026, 10, 5),
            start=time(9, 40),
            end=time(10, 10),
        )


class TestScheduleView:
    def test_typical_day_and_calendar(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "Алмаг", start_date=date(2026, 10, 6))
        prescribe(doctor, program, "st-150", start_date=date(2026, 10, 7))

        schedule = program_schedule(program)

        assert [(r.label, r.start) for r in schedule.typical] == [
            ("Группа Эрго общая", 550),
            ("st-150", 780),
        ]
        assert [c.procedure.name for c in schedule.columns] == ["Эрго общая", "st-150"]
        # Типичный день по дневной сетке слотов: занятия на своих строках, остальное — окна.
        assert len(schedule.day) == 10
        assert [(r.start, r.label) for r in schedule.day if not r.is_window] == [
            (550, "Группа Эрго общая"),
            (780, "st-150"),
        ]
        assert schedule.day[1].is_window and schedule.day[1].start == 590
        first_day = schedule.calendar[0]
        assert first_day.cells[0].bookings and not first_day.cells[1].active

    def test_program_page(self, client, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "Бассейн")

        client.force_login(doctor)
        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "Типичный день" in html
        assert "окно — пациент свободен" in html
        assert "Не удалось поставить" in html
        assert "Подобрать заново" not in html, "врач расписание только смотрит"

        client.force_login(rehab)
        assert (
            "Подобрать заново"
            in client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        )

    def test_replan_button_rights(self, client, doctor, rehab, program):
        url = reverse("scheduling:program_replan", args=[program.pk])

        client.force_login(doctor)
        assert client.post(url).status_code == 403

        client.force_login(rehab)
        assert client.post(url).url == reverse("programs:detail", args=[program.pk])
        assert client.get(url).status_code == 405

    def test_list_badges(self, client, doctor, make_program):
        ok = make_program(full_name="Хороший Х.Х.", history_number="1", start_date=date.today())
        bad = make_program(full_name="Плохой П.П.", history_number="2", start_date=date.today())
        prescribe(doctor, ok, "Эрго общая")
        prescribe(doctor, bad, "Бассейн")
        client.force_login(doctor)

        html = client.get(reverse("programs:list")).content.decode()

        assert "расписание составлено" in html
        assert "конфликты расписания" in html

    def test_card_has_schedule_block(self, client, doctor, program):
        from io import BytesIO

        from openpyxl import load_workbook

        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "st-150")
        client.force_login(doctor)

        sheet = load_workbook(
            BytesIO(client.get(reverse("cards:download", args=[program.pk])).content)
        ).active

        assert (sheet["B4"].value, sheet["C4"].value, sheet["F4"].value) == (
            time(9, 10),
            "Группа Эрго общая",
            "зал ЛФК",
        )
        assert (sheet["B5"].value, sheet["C5"].value) == (time(9, 50), None), "окно"
        assert (sheet["B8"].value, sheet["C8"].value) == (time(13, 0), "st-150")


class TestReviewFixes:
    def test_replan_does_not_flood_history(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        before = Booking.history.count()

        replan(program)
        replan(program)

        assert Booking.history.count() == before

    def test_pinned_individual_survives_replan(self, doctor, program):
        individual = prescribe(doctor, program, "Индивидуальное занятие")
        booking = Booking.objects.create(
            program=program,
            prescription=individual,
            procedure=individual.procedure,
            kind="INDIVIDUAL",
            date=date(2026, 10, 6),
            start=time(11, 10),
            end=time(11, 40),
            instructor=Instructor.objects.create(short_name="Соколов", full_name="Соколов"),
            slot=InstructorSlot.objects.get(start=time(11, 10)),
            pinned=True,
        )

        prescribe(doctor, program, "Эрго общая")

        assert Booking.objects.filter(pk=booking.pk).exists()

    def test_inactive_equipment(self, doctor, program):
        Equipment.objects.filter(name="st-150").update(is_active=False)

        prescribe(doctor, program, "st-150")

        program.refresh_from_db()
        assert not program.bookings.exists()
        assert "выключен" in program.schedule_issues[0]["message"]

    def test_htmx_response_refreshes_schedule(self, client, doctor, program):
        client.force_login(doctor)

        response = client.post(
            reverse("programs:prescription_add", args=[program.pk]),
            {"procedure": proc("Эрго общая").pk, "per_day": "1", "in_card": "on"},
        )

        html = response.content.decode()
        assert '<div id="prescriptions">' in html
        assert 'id="schedule" hx-swap-oob="true"' in html
        assert "9:10" in html

    def test_not_scheduled_badge(self, client, doctor, make_program):
        program = make_program(start_date=date.today())
        Prescription.objects.create(program=program, procedure=proc("Эрго общая"))
        client.force_login(doctor)

        assert "расписание не составлено" in client.get(reverse("programs:list")).content.decode()
