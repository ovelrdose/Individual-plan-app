"""Журнал расписания (TZ.md FR-SCH-19, решение 86): кто и почему поставил пациента.

Сегодня в тестах — вторник 06.10.2026. Инструкторы вымышленные: пара 2/2 Волков (работает
05–06, 09–10) / Лебедева (07–08), Соколов — 5/2.
"""

from datetime import date, time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Preset
from apps.programs.models import Prescription
from apps.programs.services import add_prescription
from apps.scheduling import board, board_days, journal, staff_changes
from apps.scheduling.models import Booking, BookingKind, EventSource, ScheduleEvent

MON, TUE, WED, THU = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    current = {"day": TUE}
    monkeypatch.setattr(board_days, "today", lambda: current["day"])
    return current


def events(program=None) -> list[ScheduleEvent]:
    items = ScheduleEvent.objects.order_by("pk")
    return list(items.filter(program=program) if program is not None else items)


def individual(program, day: date) -> list[ScheduleEvent]:
    return [
        e for e in events(program) if e.equipment_id is None and e.date_from <= day <= e.date_to
    ]


class TestWrite:
    def test_new_patient_by_doctor(self, patient, doctor):
        """Назначение добавил врач — запись от его имени, причины — от расчёта шахматки."""
        board_days.ensure_boards()
        program = patient(start_date=MON)

        [today_event] = individual(program, TUE)
        assert (today_event.user, today_event.source) == (doctor, EventSource.MANUAL)
        assert today_event.action == "Добавлено назначение"
        assert (today_event.before, today_event.after) == ("", "Не распределены")
        assert "ставит специалист" in today_event.reason
        [tomorrow] = individual(program, WED)
        assert tomorrow.after.endswith("9:10") and tomorrow.reason.startswith("автоматически к")

    def test_unchanged_replan_writes_nothing(self, patient):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        count = ScheduleEvent.objects.count()
        from apps.scheduling.services import replan

        replan(program)
        assert ScheduleEvent.objects.count() == count

    def test_new_board_logs_only_changes(self, patient, today):
        """Новая шахматка: «как вчера» не пишется, передача напарнику — пишется с причиной."""
        board_days.ensure_boards()
        program = patient(start_date=MON)
        assert Booking.objects.get(program=program, date=WED).instructor.short_name == "Лебедева"
        today["day"] = WED
        board_days.ensure_boards()  # четверг из среды — Лебедева работает, ничего нового
        assert individual(program, THU) == []

        today["day"] = THU
        board_days.ensure_boards()  # пятница — работает Волков
        [event] = individual(program, date(2026, 10, 9))
        assert event.source == EventSource.SYSTEM and event.user is None
        assert event.action == "Составлено расписание на 09.10 — перенос с 08.10"
        assert (event.before, event.after) == ("Лебедева 9:10", "Волков 9:10")
        assert event.reason == "Лебедева в 9:10 не работает — к напарнику Волков"

    def test_manual_move(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        booking = Booking.objects.get(program=program, date=WED)

        board.move_patient(rehab, booking, booking.version, staff["sokolov"], slots["10:30"])

        event = individual(program, WED)[-1]
        assert (event.user, event.action) == (rehab, "Перенос в расписании")
        assert (event.before, event.after) == ("Лебедева 9:10", "Соколов 10:30")

    def test_not_working(self, patient, rehab, staff):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        lebedeva = staff["lebedeva"]

        staff_changes.set_not_working(rehab, lebedeva, WED)

        event = individual(program, WED)[-1]
        assert event.user == rehab and event.action == "Лебедева не работает 07.10"
        assert event.after == "Не распределены"
        assert "не работает — в «Не распределены»" in event.reason

    def test_failed_action_writes_nothing(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        booking = Booking.objects.get(program=program, date=WED)
        count = ScheduleEvent.objects.count()
        with pytest.raises(board.BoardError):
            # Волков в среду не работает.
            board.move_patient(rehab, booking, booking.version, staff["volkov"], slots["10:30"])
        assert ScheduleEvent.objects.count() == count


class TestEquipment:
    @pytest.fixture
    def st(self, procedures):
        item = procedures["equipment"].equipment
        item.window_end = time(13, 15)  # одно время, одно место
        item.save()
        return item

    def on(self, program, day: date) -> list[Booking]:
        return list(program.bookings.filter(date=day, kind=BookingKind.EQUIPMENT))

    def test_course_in_one_line_with_reason(self, make_program, doctor, procedures, st):
        board_days.ensure_boards()
        program = make_program(start_date=MON)
        add_prescription(doctor, Prescription(program=program, procedure=procedures["equipment"]))
        [event] = [e for e in events(program) if e.equipment_id == st.pk]
        assert (event.subject, event.date_from, event.before, event.after) == (
            "st-150",
            TUE,
            "",
            "13:00",
        )
        assert event.date_to == program.therapy_dates()[-1]
        assert event.reason.startswith("основное время 13:00 — подходит в")

    def test_hold_and_waiting_takes_place(self, make_program, doctor, procedures, st, rehab):
        board_days.ensure_boards()
        first = make_program(room="1", start_date=MON, full_name="Первый Тест Тестович")
        add_prescription(doctor, Prescription(program=first, procedure=procedures["equipment"]))
        second = make_program(room="2", start_date=MON, full_name="Второй Тест Тестович")
        add_prescription(doctor, Prescription(program=second, procedure=procedures["equipment"]))
        [waiting] = [e for e in events(second) if e.equipment_id]
        assert waiting.after == "Не распределён" and "мест нет (1 из 1)" in waiting.reason
        booking = self.on(first, WED)[0]

        board.hold_equipment(rehab, board.CANCELLED, WED, booking=booking, version=booking.version)

        held = events(first)[-1]
        assert (held.user, held.action, held.after) == (
            rehab,
            "Снятие с тренажёра в блок",
            "Отменён",
        )
        took = events(second)[-1]
        assert took.source == EventSource.SYSTEM and took.after == "13:00"
        assert took.action.startswith("Освободилось место на тренажёре (после правки 1п Первый)")
        # В журнале (новые сверху) правка специалиста — выше того, кого она задела.
        assert held.pk > took.pk

    def test_over_capacity_confirmed_note(self, make_program, doctor, procedures, st, rehab):
        board_days.ensure_boards()
        first = make_program(room="1", start_date=MON)
        add_prescription(doctor, Prescription(program=first, procedure=procedures["equipment"]))
        second = make_program(room="2", start_date=MON)
        prescription = add_prescription(
            doctor, Prescription(program=second, procedure=procedures["equipment"])
        )

        board.place_equipment(
            rehab, prescription, TUE, time(13, 0), board.UNPLACED, "держит 15 мин"
        )

        event = events(second)[-1]
        assert event.after == "13:00" and "подтверждено: На тренажёре" in event.reason
        assert "причина: держит 15 мин" in event.reason


class TestKeep:
    def test_purge_older_than_week(self, patient):
        program = patient(start_date=MON)
        old = ScheduleEvent.objects.create(
            program=program, subject="x", date_from=TUE, date_to=TUE, source="system", action="x"
        )
        ScheduleEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        fresh = events(program)[0]

        assert journal.purge() == 1
        assert ScheduleEvent.objects.filter(pk=fresh.pk).exists()

    def test_new_board_purges(self, patient, today):
        program = patient(start_date=MON)
        ScheduleEvent.objects.all().update(created_at=timezone.now() - timedelta(days=8))
        today["day"] = WED
        board_days.ensure_boards()
        assert all(e.created_at > timezone.now() - timedelta(days=1) for e in events(program))


class TestScreen:
    def test_rehab_sees_entries_with_filters(self, client, patient, rehab, procedures, doctor):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        client.force_login(rehab)
        url = reverse("scheduling:journal")

        page = client.get(url).content.decode()
        assert (
            "Пациентов9" in page and "Добавлено назначение" in page and "ставит специалист" in page
        )
        assert (
            "Пациентов9"
            in client.get(url, {"date": "2026-10-07", "subject": "ind"}).content.decode()
        )
        assert "Записей нет" in client.get(url, {"source": "system"}).content.decode()
        assert "Записей нет" in client.get(url, {"subject": "eq:1"}).content.decode()
        assert "Пациентов9" in client.get(url, {"program": program.pk}).content.decode()

    def test_moved_one_of_two_sessions(self, client, patient, rehab):
        """Перенос одного из двух инд.: на экране только оно, второе — «без изменений»."""
        program = patient(start_date=MON)
        ScheduleEvent.objects.create(
            program=program,
            subject="Индивидуальные",
            date_from=WED,
            date_to=WED,
            before="Волков 9:10, Соколов 10:30",
            after="Волков 9:10, Волков 10:30",
            source=EventSource.MANUAL,
            action="Перенос в расписании",
        )
        client.force_login(rehab)

        page = client.get(reverse("scheduling:journal"), {"program": program.pk}).content.decode()
        assert "без изменений: Волков 9:10" in page and "Волков 9:10, Волков 10:30" not in page

    def test_rights(self, client, patient, doctor, make_user, other_department, rehab):
        board_days.ensure_boards()
        patient(start_date=MON)
        client.force_login(doctor)
        assert client.get(reverse("scheduling:journal")).status_code == 403
        assert "bi-clock-history" not in client.get(reverse("scheduling:board")).content.decode()
        stranger = make_user("rehab2", (other_department, Preset.REHAB))
        client.force_login(stranger)
        assert "Записей нет" in client.get(reverse("scheduling:journal")).content.decode()
        client.force_login(rehab)
        assert "bi-clock-history" in client.get(reverse("scheduling:board")).content.decode()

    def test_patient_panel_history(self, client, patient, rehab):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        client.force_login(rehab)
        page = client.get(
            reverse("scheduling:board_patient"), {"date": "2026-10-06", "program": program.pk}
        ).content.decode()
        assert "История за неделю" in page and f"?program={program.pk}" in page
