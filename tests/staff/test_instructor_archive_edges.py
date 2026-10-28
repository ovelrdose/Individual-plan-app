"""Архив инструктора — граничные случаи (TZ.md FR-STF-1, решение 83).

Пара 2/2 Волков (работает 05–06.10, 09–10.10) / Лебедева (07–08.10), Соколов — 5/2.
Сегодня по умолчанию — понедельник 05.10.2026: составлены шахматки на 05.10 и 06.10, Волков
работает в оба дня.
"""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.catalog.models import GroupSession
from apps.scheduling import board, board_days, reports, staff_changes
from apps.scheduling.models import BoardPatient, Booking, BookingKind
from apps.staff import services as staff_services
from apps.staff.models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)

SUN, MON, TUE, WED = date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: MON)


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def individual(program, day: date) -> list[Booking]:
    return list(program.bookings.filter(kind=BookingKind.INDIVIDUAL, date=day))


def column_labels(rehab, day: date) -> list[str]:
    return [column.label for column in board.board(rehab, day).columns]


class TestTodayAndTomorrow:
    def test_patients_today_and_tomorrow_unplaced(self, rehab, patient, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=SUN)  # MON и TUE — дни занятий
        volkov = staff["volkov"]
        for day in (MON, TUE):
            for booking in individual(program, day):
                board.remove_patient(rehab, booking, booking.version)
            board.place_patient(rehab, program, volkov, slots["10:30"], day)
        assert [b.instructor for b in individual(program, MON)] == [volkov]
        assert [b.instructor for b in individual(program, TUE)] == [volkov]

        removed = staff_changes.delete_instructor(rehab, volkov)

        assert removed.archived and removed.partner == "Лебедева"
        for day in (MON, TUE):
            assert individual(program, day) == []
            assert BoardPatient.objects.get(program=program, date=day).unplaced == 1
        assert {(item.program.pk, item.date) for item in removed.displaced} >= {
            (program.pk, MON),
            (program.pk, TUE),
        }

    def test_partner_continues_alone_in_own_column(self, rehab, staff, slots):
        board_days.ensure_boards()
        staff_changes.delete_instructor(rehab, staff["volkov"])

        lebedeva = staff["lebedeva"]
        lebedeva.refresh_from_db()
        assert lebedeva.partner is None and lebedeva.is_active
        teams = staff_services.build_teams()
        assert [team.label for team in teams] == ["Лебедева", "Соколов"]
        # В её рабочий день колонка — только её; Волкова нет ни в одной колонке.
        assert column_labels(rehab, WED) == ["Лебедева", "Соколов"]
        assert "Волков" not in " ".join(column_labels(rehab, MON))
        # Её шаблон смены не тронут: работает в свои дни, как раньше.
        calendar = staff_services.instructor_calendars([lebedeva], WED, WED)[lebedeva.pk]
        assert calendar.is_working(WED)
        assert ShiftPattern.objects.get(instructor=lebedeva).valid_to is None

    def test_partner_bookings_untouched(self, rehab, patient, staff, slots):
        """Пациенты напарника в его днях остаются на месте."""
        board_days.ensure_boards()
        program = patient(start_date=MON)
        sokolov = staff["sokolov"]
        for booking in individual(program, TUE):
            board.remove_patient(rehab, booking, booking.version)
        board.place_patient(rehab, program, sokolov, slots["9:10"], TUE)

        staff_changes.delete_instructor(rehab, staff["volkov"])

        assert [b.instructor for b in individual(program, TUE)] == [sokolov]


class TestEndedRecords:
    def test_future_records_removed_past_kept(self, rehab, staff, slots, procedures):
        volkov = staff["volkov"]
        led = GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))
        past = ShiftException.objects.create(instructor=volkov, date=SUN, is_working=True)
        future = ShiftException.objects.create(instructor=volkov, date=WED, is_working=True)
        started = InstructorDuty.objects.create(
            instructor=volkov,
            slot=slots["9:10"],
            kind="GROUP_LEAD",
            group_session=led,
            valid_from=SUN,
        )
        later = InstructorDuty.objects.create(
            instructor=volkov, slot=slots["11:10"], kind="METHOD_WORK", valid_from=WED
        )
        block_today = InstructorBlock.objects.create(
            instructor=volkov, slot=slots["10:30"], date=MON, kind="BOS"
        )
        block_later = InstructorBlock.objects.create(
            instructor=volkov,
            slot=slots["9:10"],
            date=TUE,
            kind="GROUP_LEAD",
            group_session=led,
        )

        removed = staff_changes.delete_instructor(rehab, volkov)

        assert removed.archived and removed.ended >= 2  # шаблон смены и распорядок
        assert ShiftException.objects.filter(pk=past.pk).exists()
        assert not ShiftException.objects.filter(pk=future.pk).exists()
        started.refresh_from_db()
        assert started.valid_to == MON  # «Ведение группы» заканчивается сегодня
        assert not InstructorDuty.objects.filter(pk=later.pk).exists()
        assert InstructorBlock.objects.filter(pk=block_today.pk).exists()
        assert not InstructorBlock.objects.filter(pk=block_later.pk).exists()
        assert ShiftPattern.objects.get(instructor=volkov).valid_to == MON

    def test_pattern_starting_later_deleted(self, rehab, staff):
        sokolov = staff["sokolov"]
        ShiftPattern.objects.filter(instructor=sokolov).update(valid_to=TUE)
        ShiftPattern.objects.create(
            instructor=sokolov, pattern="daily", anchor_date=WED, valid_from=WED
        )

        staff_changes.delete_instructor(rehab, sokolov)

        assert list(
            ShiftPattern.objects.filter(instructor=sokolov).values_list("valid_to", flat=True)
        ) == [MON]

    def test_past_bookings_kept(self, rehab, patient, staff, slots):
        board_days.ensure_boards(SUN)  # шахматка на пятницу не нужна — запись прошлого дня
        program = patient(start_date=date(2026, 10, 1))
        past = Booking.objects.create(
            program=program,
            prescription=program.prescriptions.get(),
            procedure=program.prescriptions.get().procedure,
            kind=BookingKind.INDIVIDUAL,
            date=date(2026, 10, 2),
            instructor=staff["volkov"],
            slot=slots["9:10"],
            start=time(9, 10),
            end=time(9, 40),
        )

        staff_changes.delete_instructor(rehab, staff["volkov"])

        assert Booking.objects.filter(pk=past.pk).exists()
        # Прошлое не пропадает с экранов: колонка в шахматке за 02.10 и строка в загрузке.
        past_board = board.board(rehab, date(2026, 10, 2))
        assert "Волков" in [column.label for column in past_board.columns]
        load = reports.instructor_load(rehab, date(2026, 10, 1), date(2026, 10, 2))
        assert [(row.name, row.sessions) for row in load.rows if row.name == "Волков"] == [
            ("Волков", 1)
        ]

    def test_already_archived(self, client, rehab, staff):
        volkov = staff["volkov"]
        staff_changes.delete_instructor(rehab, volkov)
        volkov.refresh_from_db()
        with pytest.raises(staff_services.StaffError, match="уже в архиве"):
            staff_changes.delete_instructor(rehab, volkov)
        client.force_login(rehab)
        html = htmx(client, reverse("staff:instructor_delete", args=[volkov.pk]), {"confirm": 1})
        assert "уже в архиве" in html

    def test_history_has_author(self, rehab, staff):
        volkov = staff["volkov"]
        staff_changes.delete_instructor(rehab, volkov)
        assert volkov.history.first().history_user == rehab
        lebedeva = staff["lebedeva"]
        assert lebedeva.history.first().history_user == rehab  # разрыв пары — тоже автор


class TestRestore:
    def test_restore_then_shift_and_partner_again(self, client, rehab, staff, slots):
        volkov, lebedeva = staff["volkov"], staff["lebedeva"]
        staff_changes.delete_instructor(rehab, volkov)
        client.force_login(rehab)

        html = htmx(client, reverse("staff:instructor_restore", args=[volkov.pk]))

        volkov.refresh_from_db()
        assert volkov.is_active and volkov.partner is None
        assert "снова работает" in html
        assert volkov in staff_services.active_instructors()
        # Без смены колонки в шахматке нет.
        board_days.ensure_boards()
        assert "Волков" not in " ".join(column_labels(rehab, TUE))

        staff_services.save_instructor_card(rehab, volkov, lebedeva)
        staff_services.change_shift_pattern(rehab, volkov, "2/2", date(2026, 10, 9), WED)
        lebedeva.refresh_from_db()
        assert lebedeva.partner == volkov
        assert "Волков/ Лебедева" in [t.label for t in staff_services.build_teams()]

    def test_archived_cannot_get_partner(self, rehab, staff):
        volkov = staff["volkov"]
        staff_changes.delete_instructor(rehab, volkov)
        volkov.refresh_from_db()
        with pytest.raises(staff_services.StaffError, match="в архиве"):
            staff_services.save_instructor_card(rehab, volkov, staff["sokolov"])
        with pytest.raises(staff_services.StaffError, match="действующий"):
            staff_services.save_instructor_card(rehab, staff["sokolov"], volkov)

    def test_archived_not_offered_as_partner(self, client, rehab, staff):
        volkov = staff["volkov"]
        staff_changes.delete_instructor(rehab, volkov)
        client.force_login(rehab)
        sokolov = staff["sokolov"]
        html = htmx(
            client,
            reverse("staff:instructor_edit", args=[sokolov.pk]),
            {
                "ins-short_name": "Соколов",
                "ins-full_name": "Соколов С.С.",
                "ins-partner": volkov.pk,
                "ins-display_order": "1",
            },
        )
        sokolov.refresh_from_db()
        assert sokolov.partner is None
        assert "is-invalid" in html or "Выберите корректный вариант" in html


class TestSameSurname:
    def test_new_instructor_with_archived_surname_refused(self, client, rehab, staff):
        volkov = staff["volkov"]
        staff_changes.delete_instructor(rehab, volkov)
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("staff:instructor_add"),
            {"ins-short_name": "Волков", "ins-full_name": "Волков Д.Д.", "ins-display_order": "0"},
        )

        assert Instructor.objects.filter(short_name__iexact="волков").count() == 1
        assert "уже" in html  # понятный текст вместо 500

    def test_case_insensitive_duplicate(self, rehab, staff):
        with pytest.raises(staff_services.StaffError, match="уже есть"):
            staff_services.save_instructor_card(
                rehab, Instructor(short_name=" волков ", full_name="Волков Д.Д."), None
            )
        assert Instructor.objects.filter(short_name__iexact="волков").count() == 1

    def test_empty_name_is_form_error(self, client, rehab, slots):
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("staff:instructor_add"),
            {"ins-short_name": "  ", "ins-full_name": "", "ins-display_order": "0"},
        )
        assert "Обязательное поле" in html
        assert not Instructor.objects.exists()


class TestRights:
    @pytest.mark.parametrize("name", ["instructor_delete", "instructor_restore", "instructor_edit"])
    def test_doctor_post_forbidden_nothing_changes(self, client, doctor, rehab, staff, name):
        volkov = staff["volkov"]
        if name == "instructor_restore":
            staff_changes.delete_instructor(rehab, volkov)
        volkov.refresh_from_db()
        before = (volkov.is_active, volkov.short_name, volkov.partner_id)
        client.force_login(doctor)

        response = client.post(
            reverse(f"staff:{name}", args=[volkov.pk]),
            {
                "confirm": "1",
                "ins-short_name": "Взлом",
                "ins-full_name": "В",
                "ins-display_order": 0,
            },
        )

        assert response.status_code == 403
        volkov.refresh_from_db()
        assert (volkov.is_active, volkov.short_name, volkov.partner_id) == before

    def test_doctor_add_forbidden(self, client, doctor, slots):
        client.force_login(doctor)
        response = client.post(
            reverse("staff:instructor_add"),
            {"ins-short_name": "Новиков", "ins-full_name": "Н", "ins-display_order": 0},
        )
        assert response.status_code == 403
        assert not Instructor.objects.filter(short_name="Новиков").exists()

    def test_anonymous_redirected(self, client, staff):
        response = client.post(
            reverse("staff:instructor_delete", args=[staff["volkov"].pk]), {"confirm": "1"}
        )
        assert response.status_code == 302 and "/login/" in response["Location"]
        staff["volkov"].refresh_from_db()
        assert staff["volkov"].is_active

    def test_service_requires_right(self, doctor, staff):
        from django.core.exceptions import PermissionDenied

        with pytest.raises(PermissionDenied):
            staff_changes.delete_instructor(doctor, staff["volkov"])
        with pytest.raises(PermissionDenied):
            staff_changes.restore_instructor(doctor, staff["volkov"])
