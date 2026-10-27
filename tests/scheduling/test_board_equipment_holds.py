"""Блоки тренажёров в шахматке: «Не распределены» и «Отменены» (TZ.md FR-SCH-11a, решение 84).

Сегодня в тестах — вторник 06.10.2026. Окно st-150 сужено до 13:00–13:30 (два времени по
15 минут), вместимость 1: третий пациент на тренажёр не помещается.
"""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.accounts.models import Preset
from apps.catalog.models import Equipment, GroupSession, Procedure, ProcedureKind
from apps.programs.models import Prescription, WithdrawalReason
from apps.programs.services import add_prescription, withdraw
from apps.scheduling import board, board_days, manual, services
from apps.scheduling.domain.model import IssueCode
from apps.scheduling.models import (
    BoardPatient,
    Booking,
    BookingKind,
    RemovedKind,
    RemovedSession,
)
from apps.scheduling.services import program_schedule, replan

MON, TUE, WED, THU = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def st(procedures) -> Equipment:
    item = procedures["equipment"].equipment
    item.window_end = time(13, 30)
    item.save()
    return item


@pytest.fixture
def patient(doctor, make_program, procedures, st):
    """make: пациент со st-150 (курс с 05.10, если не задан другой)."""

    def make(room: str, **fields):
        program = make_program(room=room, full_name=f"Пациентов{room} Тест Тестович", **fields)
        add_prescription(doctor, Prescription(program=program, procedure=procedures["equipment"]))
        return program

    return make


@pytest.fixture
def three(patient):
    """Двое на тренажёре, третьему места нет."""
    board_days.ensure_boards()
    return patient("1"), patient("2"), patient("3")


def on(program, day: date) -> list[Booking]:
    return list(program.bookings.filter(date=day, kind=BookingKind.EQUIPMENT).order_by("start"))


def holds(user, day: date, index: int = 0) -> tuple[list[str], list[str]]:
    item = board.board(user, day).equipment_holds[index]
    return [p.label for p, _u in item.unplaced], [p.label for p, _u in item.cancelled]


def prescription(program) -> Prescription:
    return program.prescriptions.get(procedure__kind=ProcedureKind.EQUIPMENT)


class TestRows:
    def test_overflow_goes_to_unplaced(self, three, rehab):
        _first, _second, third = three
        assert on(third, TUE) == []
        assert holds(rehab, TUE) == (["3п Пациентов3"], [])
        third.refresh_from_db()
        assert any(i["code"] == IssueCode.EQUIPMENT_UNPLACED for i in third.schedule_issues)

    def test_new_equipment_gets_own_cells(self, three, rehab, doctor):
        imitron = Equipment.objects.create(name="Имитрон", capacity=3)
        procedure = Procedure.objects.create(
            name="Имитрон",
            card_label="Имитрон",
            kind=ProcedureKind.EQUIPMENT,
            equipment=imitron,
            default_duration_min=15,
        )
        add_prescription(doctor, Prescription(program=three[0], procedure=procedure))

        result = board.board(rehab, TUE)

        assert [h.equipment.name for h in result.equipment_holds] == ["st-150", "Имитрон"]
        assert holds(rehab, TUE, 1) == ([], [])

    def test_removed_on_program_page_is_not_shown(self, three, rehab):
        booking = on(three[0], TUE)[0]
        manual.remove_booking(rehab, booking, booking.version)
        unplaced, _cancelled = holds(rehab, TUE)
        assert "1п Пациентов1" not in unplaced

    def test_public_board_has_no_rows(self, client, three, settings):
        settings.PUBLIC_BOARD_NETWORKS = []
        page = client.get(reverse("public_board"), {"date": "2026-10-06"}).content.decode()
        assert "1п" in page and "Не распр." not in page


class TestPlace:
    def test_over_capacity_this_and_following_with_reason(self, three, rehab):
        """Сверх вместимости — на эту дату и все следующие, причина — пометкой (решение 85)."""
        first, _second, third = three
        start = on(first, TUE)[0].start
        with pytest.raises(board.ConfirmationRequired, match="вместимости") as asked:
            board.place_equipment(rehab, prescription(third), TUE, start, board.UNPLACED)
        # Одно сообщение на все дни, а не по строке на каждый день курса.
        assert len(asked.value.violations) == 1 and "06.10, 07.10" in str(asked.value)

        result = board.place_equipment(
            rehab, prescription(third), TUE, start, board.UNPLACED, "на 10 минут"
        )

        assert [(b.start, b.pinned, b.note) for b in on(third, TUE)] == [
            (start, True, "на 10 минут")
        ]
        assert [(b.start, b.pinned, b.note) for b in on(third, THU)] == [
            (start, True, "на 10 минут")
        ]
        assert TUE in result.changed and THU in result.changed and not result.skipped
        assert holds(rehab, WED) == ([], [])

    def test_over_capacity_confirmed_without_reason(self, three, rehab):
        first, _second, third = three
        start = on(first, TUE)[0].start

        board.place_equipment(
            rehab, prescription(third), TUE, start, board.UNPLACED, confirmed=True
        )

        assert [(b.start, b.note) for b in on(third, WED)] == [(start, "")]

    def test_busy_following_day_is_skipped(self, three, rehab, doctor, procedures):
        """Дни, где пациент в это время занят, пропускаются и сверх вместимости."""
        first, _second, third = three
        start = on(first, TUE)[0].start
        GroupSession.objects.create(procedure=procedures["group"], start_time=start)
        add_prescription(
            doctor, Prescription(program=third, procedure=procedures["group"], start_date=WED)
        )

        result = board.place_equipment(
            rehab, prescription(third), TUE, start, board.UNPLACED, confirmed=True
        )

        assert result.changed == [TUE]
        assert {WED, THU} <= {d for d, _r in result.skipped}
        assert holds(rehab, WED)[0] == ["3п Пациентов3"]

    def test_into_freed_time_this_and_following(self, three, rehab):
        first, _second, third = three
        booking = on(first, TUE)[0]
        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)
        # Сегодня ждущего ставит специалист: подбор занял место только с завтрашнего дня.
        assert on(third, TUE) == [] and on(third, WED)
        third.bookings.filter(kind=BookingKind.EQUIPMENT).delete()
        RemovedSession.objects.filter(prescription__program=third).delete()

        result = board.place_equipment(
            rehab, prescription(third), TUE, booking.start, board.UNPLACED
        )

        assert result.changed[0] == TUE and THU in result.changed
        assert all(b.pinned and b.start == booking.start for b in on(third, THU))

    def test_already_on_equipment_is_error(self, three, rehab):
        third = three[2]
        board.place_equipment(rehab, prescription(third), TUE, time(13, 15), board.UNPLACED, "x")
        with pytest.raises(board.BoardError, match="уже стоит"):
            board.place_equipment(rehab, prescription(third), TUE, time(13, 0), board.UNPLACED)

    def test_rights_and_dates(self, three, rehab, doctor, make_user, other_department):
        item = prescription(three[2])
        with pytest.raises(board.BoardError, match="Нет права"):
            board.place_equipment(doctor, item, TUE, time(13, 0), board.UNPLACED)
        stranger = make_user("rehab2", (other_department, Preset.REHAB))
        with pytest.raises(board.BoardError, match="другого отделения"):
            board.place_equipment(stranger, item, TUE, time(13, 0), board.UNPLACED)
        with pytest.raises(board.BoardError, match="сегодняшнее и завтрашнее"):
            board.place_equipment(rehab, item, MON, time(13, 0), board.UNPLACED)


class TestHold:
    def test_cancelled_from_this_date_on(self, three, rehab):
        first = three[0]
        booking = on(first, TUE)[0]

        result = board.hold_equipment(
            rehab, board.CANCELLED, TUE, booking=booking, version=booking.version
        )

        assert TUE in result.changed and THU in result.changed
        assert not first.bookings.filter(kind=BookingKind.EQUIPMENT, date__gte=TUE).exists()
        assert first.bookings.filter(kind=BookingKind.EQUIPMENT, date=MON).count() == 0
        assert holds(rehab, TUE)[1] == ["1п Пациентов1"]
        assert holds(rehab, WED)[1] == ["1п Пациентов1"]
        # Подбор отменённого не возвращает, и это не конфликт.
        replan(first)
        assert not first.bookings.filter(kind=BookingKind.EQUIPMENT, date__gte=TUE).exists()
        first.refresh_from_db()
        assert not any(i["code"] == IssueCode.EQUIPMENT_UNPLACED for i in first.schedule_issues)

    def test_return_from_cancelled(self, three, rehab):
        first = three[0]
        booking = on(first, TUE)[0]
        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)
        # Место первого с завтрашнего дня занял ждущий — дальше это сверх вместимости.
        with pytest.raises(board.ConfirmationRequired):
            board.place_equipment(rehab, prescription(first), TUE, booking.start, board.CANCELLED)
        board.place_equipment(
            rehab, prescription(first), TUE, booking.start, board.CANCELLED, confirmed=True
        )

        assert [b.start for b in on(first, TUE)] == [booking.start]
        assert [b.start for b in on(first, WED)] == [booking.start]
        assert not RemovedSession.objects.filter(
            prescription__program=first, kind=RemovedKind.CANCELLED
        ).exists()
        assert holds(rehab, WED) == ([], [])

    def test_held_in_unplaced_is_not_placed_automatically(self, three, rehab):
        first = three[0]
        booking = on(first, TUE)[0]
        board.hold_equipment(rehab, board.UNPLACED, TUE, booking=booking, version=booking.version)

        replan(first)

        assert on(first, TUE) == [] and on(first, WED) == []
        assert "1п Пациентов1" in holds(rehab, TUE)[0]

    def test_between_blocks(self, three, rehab):
        third = three[2]
        board.hold_equipment(
            rehab, board.CANCELLED, TUE, prescription=prescription(third), source=board.UNPLACED
        )
        assert holds(rehab, TUE) == ([], ["3п Пациентов3"])
        board.hold_equipment(
            rehab, board.UNPLACED, TUE, prescription=prescription(third), source=board.CANCELLED
        )
        assert holds(rehab, TUE) == (["3п Пациентов3"], [])
        with pytest.raises(board.BoardError, match="уже в этом блоке"):
            board.hold_equipment(
                rehab, board.UNPLACED, TUE, prescription=prescription(third), source=board.UNPLACED
            )

    def test_calendar_shows_block(self, three, rehab):
        first = three[0]
        booking = on(first, TUE)[0]
        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)
        calendar = program_schedule(first).calendar
        row = next(r for r in calendar if r.date == WED)
        assert [c.hold for c in row.cells] == ["отменён"]


class TestRefill:
    def test_earlier_admission_first(self, patient, rehab, st):
        st.window_end = time(13, 15)
        st.save()
        board_days.ensure_boards()
        first, later = patient("1"), patient("2")
        earlier = patient("3", start_date=date(2026, 10, 3))
        assert on(later, WED) == [] and on(earlier, WED) == []
        booking = on(first, TUE)[0]

        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)

        assert on(earlier, WED) and on(later, WED) == []
        assert on(earlier, TUE) == []

    def test_withdrawal_frees_place(self, three, doctor):
        first, _second, third = three
        withdraw(doctor, first, WED, WithdrawalReason.EARLY_DISCHARGE)
        assert on(third, WED) and on(third, TUE) == []

    def test_one_place_replans_one_waiting(self, patient, rehab, st, monkeypatch):
        """Место одно — пересобирается один ждущий, а не все: пересборка дорогая."""
        st.window_end = time(13, 15)
        st.save()
        board_days.ensure_boards()
        first = patient("1")
        waiting = [patient(str(room)) for room in range(2, 6)]
        replanned = []
        original = services.replan

        def spy(program, **kwargs):
            if not kwargs.get("refill", True):
                replanned.append(program.pk)
            return original(program, **kwargs)

        monkeypatch.setattr(services, "replan", spy)
        booking = on(first, TUE)[0]

        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)

        assert replanned == [waiting[0].pk]
        assert on(waiting[0], WED) and all(on(p, WED) == [] for p in waiting[1:])

    def test_no_free_place_on_missing_days_no_replan(self, three, monkeypatch):
        replanned = []
        monkeypatch.setattr(services, "replan", lambda program, **kw: replanned.append(program))
        assert services.refill_equipment({three[0].bookings.first().equipment_id}) == []
        assert replanned == []

    def test_busy_waiting_does_not_block_next(self, patient, rehab, st, doctor, procedures):
        """Первому ждущему окно закрывает группа — место получает следующий."""
        st.window_end = time(13, 15)
        st.save()
        GroupSession.objects.create(procedure=procedures["group"], start_time=time(13, 0))
        board_days.ensure_boards()
        first = patient("1")
        busy = patient("2", start_date=date(2026, 10, 3))
        add_prescription(doctor, Prescription(program=busy, procedure=procedures["group"]))
        next_one = patient("3")
        booking = on(first, TUE)[0]

        board.hold_equipment(rehab, board.CANCELLED, TUE, booking=booking, version=booking.version)

        assert on(busy, WED) == [] and on(next_one, WED)

    def test_batch_replan_refills_once(self, three, procedures, doctor, monkeypatch):
        for program in three:
            add_prescription(doctor, Prescription(program=program, procedure=procedures["group"]))
        calls = []
        monkeypatch.setattr(
            services, "refill_equipment", lambda ids, source=None: calls.append(ids)
        )
        GroupSession.objects.create(procedure=procedures["group"], start_time=time(10, 0))

        services.replan_for_group(procedures["group"].pk)

        assert calls == [{st_id(three[0])}]


def st_id(program) -> int:
    return prescription(program).procedure.equipment_id


class TestJournal:
    def test_removal_written_once_with_author(self, three, rehab):
        booking = on(three[0], WED)[0]
        board.hold_equipment(rehab, board.CANCELLED, WED, booking=booking, version=booking.version)
        removal = RemovedSession.objects.get(prescription__program=three[0], date=WED)
        rows = list(removal.history.all())
        assert len(rows) == 1 and rows[0].history_user == rehab

    def test_replan_without_changes_does_not_touch_program(self, three):
        program = three[2]
        before = program.history.count()
        replan(program)
        assert program.history.count() == before


class TestSyncProgram:
    def test_patient_without_individual_skips_board_recompute(self, three, monkeypatch):
        calls = []
        monkeypatch.setattr(board_days, "_recompute", lambda *a, **kw: calls.append(a))
        board_days.sync_program(three[0])
        assert calls == []

    def test_patient_with_individual_is_recomputed(self, three, doctor, procedures, monkeypatch):
        add_prescription(doctor, Prescription(program=three[0], procedure=procedures["individual"]))
        calls = []
        monkeypatch.setattr(board_days, "_recompute", lambda *a, **kw: calls.append(a))
        board_days.sync_program(three[0])
        assert calls

    def test_patient_only_in_board_block_is_recomputed(self, three, monkeypatch):
        """Индивидуальное удалили, а пациент ещё в блоке шахматки — сверка нужна."""
        BoardPatient.objects.create(date=TUE, program=three[0], unplaced=1)
        calls = []
        monkeypatch.setattr(board_days, "_recompute", lambda *a, **kw: calls.append(a))
        board_days.sync_program(three[0])
        assert calls


class TestMove:
    def test_skips_day_edited_separately(self, three, rehab):
        first = three[0]
        booking = on(first, TUE)[0]
        other = time(13, 15) if booking.start == time(13, 0) else time(13, 0)
        wed = on(first, WED)[0]
        manual.edit_booking(rehab, wed, wed.version, manual.Target(start=time(14, 0)), reason="x")
        booking = on(first, TUE)[0]  # пересборка пересоздала автоматическую запись

        result = board.move_equipment(rehab, booking, booking.version, other, "x")

        assert on(first, WED)[0].start == time(14, 0)
        assert WED in [d for d, _r in result.skipped]
        assert on(first, THU)[0].start == other or THU in [d for d, _r in result.skipped]


class TestScreen:
    def test_rows_and_chips(self, client, three, rehab):
        client.force_login(rehab)
        page = client.get(reverse("scheduling:board"), {"date": "2026-10-06"}).content.decode()
        assert 'data-hold="unplaced"' in page and 'data-hold="cancelled"' in page
        assert f'data-prescription="{prescription(three[2]).pk}"' in page

    def test_place_and_hold(self, client, three, rehab):
        client.force_login(rehab)
        first, _second, third = three
        booking = on(first, TUE)[0]

        done = client.post(
            reverse("scheduling:board_equipment_hold"),
            {"target": "cancelled", "booking": booking.pk, "version": booking.version},
        ).content.decode()
        assert "в блоке «Отменены»" in done and 'hx-swap-oob="true"' in done

        url = reverse("scheduling:board_equipment_place")
        data = {
            "prescription": prescription(third).pk,
            "date": "2026-10-06",
            "start": f"{booking.start:%H:%M}",
            "source": "unplaced",
        }
        assert "Пациент поставлен" in client.post(url, data).content.decode()

        asked = client.post(url, {**data, "prescription": prescription(first).pk,
                                  "source": "cancelled"}).content.decode()  # fmt: skip
        assert 'name="reason"' in asked and "вместимости" in asked

    def test_doctor_forbidden(self, client, three, doctor):
        client.force_login(doctor)
        page = client.get(reverse("scheduling:board"), {"date": "2026-10-06"}).content.decode()
        assert 'data-hold="unplaced"' not in page and "Не распр." in page
        response = client.post(
            reverse("scheduling:board_equipment_hold"),
            {"target": "cancelled", "prescription": prescription(three[2]).pk,
             "source": "unplaced", "date": "2026-10-06"},
        )  # fmt: skip
        assert response.status_code == 403
