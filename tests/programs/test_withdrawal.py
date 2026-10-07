"""Выбытие и восстановление пациента (TZ.md FR-PRG-9, решение 62).

Сегодня в тестах — вторник 06.10.2026 (следующий будний — среда 07.10); курс ШРМ 4 с 05.10
по 19.10. Инструкторы вымышленные (фикстуры шахматки).
"""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts.models import Role
from apps.cards.services import build_card_data
from apps.programs import services
from apps.programs.models import Prescription, Withdrawal, WithdrawalReason
from apps.programs.services import Period, ProgramsError, add_prescription
from apps.scheduling import board, board_days, manual
from apps.scheduling.models import BoardPatient, Booking, BookingKind

MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (5, 6, 7, 8, 9))
REFUSAL = WithdrawalReason.REFUSAL


@pytest.fixture(autouse=True)
def today(monkeypatch):
    current = {"day": TUE}
    monkeypatch.setattr(board_days, "today", lambda: current["day"])
    return current


@pytest.fixture
def program(doctor, make_program, procedures, staff):
    """Пациент со st-150 и индивидуальным, курс с 05.10."""
    item = make_program(room="9", full_name="Выбывающий Тест Тестович")
    for key in ("equipment", "individual"):
        add_prescription(doctor, Prescription(program=item, procedure=procedures[key]))
    board_days.ensure_boards()
    return item


def kinds_on(program, day: date) -> set[str]:
    return set(Booking.objects.filter(program=program, date=day).values_list("kind", flat=True))


class TestWithdraw:
    def test_lessons_from_date_are_removed_past_kept(self, program, rehab):
        # Сегодняшняя шахматка сама не заполняется (FR-SCH-9): индивидуальное — на завтра.
        assert BoardPatient.objects.get(program=program, date=TUE).unplaced == 1
        assert kinds_on(program, WED) == {BookingKind.EQUIPMENT, BookingKind.INDIVIDUAL}

        services.withdraw(rehab, program, TUE, REFUSAL, "уехал домой")

        assert not Booking.objects.filter(program=program, date__gte=TUE).exists()
        assert not BoardPatient.objects.filter(program=program).exists()
        assert all(c.program.pk != program.pk for c in board.programs_for_cell(rehab, WED))

    def test_board_holds_are_cleared(self, program, rehab):
        booking = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        board.remove_patient(rehab, booking, booking.version)
        assert BoardPatient.objects.filter(program=program, date=WED, cancelled=1).exists()

        services.withdraw(rehab, program, TUE, REFUSAL)

        for day in (TUE, WED):
            result = board.board(rehab, day)
            assert result.cancelled == [] and result.unplaced == []

    def test_future_date_keeps_lessons_before_it(self, program, doctor):
        services.withdraw(doctor, program, THU, WithdrawalReason.TRANSFER)
        assert BookingKind.EQUIPMENT in kinds_on(program, WED)
        assert not Booking.objects.filter(program=program, date__gte=THU).exists()

    @pytest.mark.parametrize("day", [MON, date(2026, 10, 19), date(2026, 10, 25)])
    def test_date_inside_course(self, program, rehab, day):
        with pytest.raises(ProgramsError, match="Дата выбытия"):
            services.withdraw(rehab, program, day, REFUSAL)

    def test_twice_and_bad_reason(self, program, rehab):
        with pytest.raises(ProgramsError, match="причину"):
            services.withdraw(rehab, program, TUE, "nonsense")
        services.withdraw(rehab, program, TUE, REFUSAL)
        with pytest.raises(ProgramsError, match="уже"):
            services.withdraw(rehab, program, WED, REFUSAL)

    def test_rights(self, program, make_user, other_department):
        stranger = make_user("rehab2", (other_department, Role.REHAB))
        with pytest.raises(PermissionDenied):
            services.withdraw(stranger, program, TUE, REFUSAL)

    def test_withdrawn_is_read_only(self, program, doctor, rehab):
        services.withdraw(rehab, program, TUE, REFUSAL)
        program.refresh_from_db()
        assert not services.can_edit(doctor, Withdrawal.objects.get().program)
        item = program.prescriptions.first()
        with pytest.raises(ProgramsError, match="выбыл"):
            services.update_prescription(doctor, item)
        with pytest.raises(ProgramsError, match="выбыл"):
            services.save_program(doctor, program, end_date_changed=False)

    def test_schedule_edit_on_absent_day_is_refused(self, program, rehab):
        services.withdraw(rehab, program, WED, REFUSAL)
        booking = Booking.objects.get(program=program, date=TUE, kind=BookingKind.EQUIPMENT)
        with pytest.raises(manual.EditError, match="выбыл"):
            manual.check(
                program, booking.prescription, WED, booking.start, booking.end,
                equipment=booking.equipment,
            )  # fmt: skip


class TestRestore:
    def test_restore_later_keeps_gap_empty(self, program, rehab, today):
        services.withdraw(rehab, program, TUE, REFUSAL)
        today["day"] = THU
        board_days.ensure_boards()

        services.restore(rehab, program)

        withdrawal = Withdrawal.objects.get()
        assert (withdrawal.date_from, withdrawal.returned_on) == (TUE, THU)
        assert not Booking.objects.filter(program=program, date__in=[TUE, WED]).exists()
        assert BookingKind.EQUIPMENT in kinds_on(program, THU)
        # Сегодня — в «Не распределены», завтра — автоматически в сетку.
        assert BoardPatient.objects.get(program=program, date=THU).unplaced == 1
        assert BookingKind.INDIVIDUAL in kinds_on(program, FRI)

    def test_restore_same_day_drops_mark(self, program, rehab):
        services.withdraw(rehab, program, TUE, REFUSAL)
        services.restore(rehab, program)
        assert not Withdrawal.objects.exists()
        assert kinds_on(program, TUE) >= {BookingKind.EQUIPMENT}

    def test_after_course_is_archive(self, program, rehab, today):
        services.withdraw(rehab, program, TUE, REFUSAL)
        with pytest.raises(ProgramsError, match="архиве"):
            services.restore(rehab, program, today=date(2026, 10, 19))

    def test_not_withdrawn(self, program, rehab):
        with pytest.raises(ProgramsError, match="не отмечен"):
            services.restore(rehab, program)


class TestListAndCard:
    def test_filters(self, program, doctor, department):
        def listed(period):
            return [
                p.pk for p in services.programs_for(doctor, department, period=period, today=TUE)
            ]

        assert listed(Period.CURRENT) == [program.pk] and listed(Period.WITHDRAWN) == []
        services.withdraw(doctor, program, WED, REFUSAL)
        assert listed(Period.CURRENT) == []
        assert listed(Period.FINISHED) == [program.pk] == listed(Period.WITHDRAWN)

    def test_card_hatches_absent_days(self, program, rehab):
        services.withdraw(rehab, program, THU, REFUSAL)
        program.refresh_from_db()
        fresh = type(program).objects.get(pk=program.pk)
        data = build_card_data(fresh)
        assert data.withdrawn_on == THU
        assert {THU, FRI, date(2026, 10, 17)} <= data.rest_dates
        assert WED not in data.rest_dates


class TestScreen:
    def test_withdraw_and_restore_from_page(self, client, program, rehab):
        client.force_login(rehab)
        url = reverse("programs:detail", args=[program.pk])
        assert "Пациент выбыл раньше срока" in client.get(url).content.decode()

        client.post(
            reverse("programs:withdraw", args=[program.pk]),
            {"date_from": "2026-10-07", "reason": REFUSAL, "note": "уехал"},
        )
        page = client.get(url).content.decode()
        assert "Пациент выбыл с 07.10.2026" in page and "Восстановить" in page

        client.post(reverse("programs:restore", args=[program.pk]))
        assert not Withdrawal.objects.exists()

    def test_bad_date_shows_message(self, client, program, rehab):
        client.force_login(rehab)
        response = client.post(
            reverse("programs:withdraw", args=[program.pk]),
            {"date_from": "2026-11-30", "reason": REFUSAL},
            follow=True,
        )
        assert "Дата выбытия" in response.content.decode() and not Withdrawal.objects.exists()

    def test_list_badge(self, client, program, doctor):
        services.withdraw(doctor, program, WED, REFUSAL)
        client.force_login(doctor)
        page = client.get(reverse("programs:list"), {"period": "withdrawn"}).content.decode()
        assert "Выбыл 07.10 (отказ)" in page
