"""Шахматка на день вперёд (TZ.md FR-SCH-5…13): составление, перенос, блоки, правка.

Сегодня в тестах — вторник 06.10.2026, следующий будний — среда 07.10. Инструкторы
вымышленные: пара 2/2 Волков (работает 05–06, 09–10) / Лебедева (07–08), Соколов — 5/2.
"""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure, ProcedureKind
from apps.programs.models import Prescription
from apps.programs.services import add_prescription, update_prescription
from apps.scheduling import board, board_days, manual, staff_changes
from apps.scheduling.models import BoardDay, BoardPatient, Booking, BookingKind
from apps.staff.models import Instructor, ShiftException

MON, TUE, WED, THU = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    current = {"day": TUE}
    monkeypatch.setattr(board_days, "today", lambda: current["day"])
    return current


def seats(program, day: date) -> list[tuple[str, str, str]]:
    return [
        (b.instructor.short_name, f"{b.start:%-H:%M}", b.note)
        for b in Booking.objects.filter(program=program, date=day, kind=BookingKind.INDIVIDUAL)
        .select_related("instructor")
        .order_by("start")
    ]


def holds(program, day: date) -> tuple[int, int]:
    entry = BoardPatient.objects.filter(program=program, date=day).first()
    return (entry.unplaced, entry.cancelled) if entry else (0, 0)


class TestBoardDates:
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            (TUE, [TUE, WED]),
            (date(2026, 10, 9), [date(2026, 10, 9), date(2026, 10, 12)]),
            (date(2026, 10, 10), [date(2026, 10, 12)]),
        ],
    )
    def test_today_and_next_weekday(self, db, today, current, expected):
        today["day"] = current
        board_days.ensure_boards()
        assert sorted(BoardDay.objects.values_list("date", flat=True)) == expected

    def test_editable_only_today_and_next(self, db):
        board_days.ensure_boards()
        assert board_days.editable(TUE) and board_days.editable(WED)
        assert not board_days.editable(MON)
        assert not board_days.editable(THU)


class TestNewPatient:
    def test_admitted_today_is_placed_tomorrow_only(self, patient):
        """Поступил сегодня: сегодня занятий нет, завтра — сам в свободном окне (FR-SCH-7)."""
        board_days.ensure_boards()
        program = patient(start_date=TUE)

        assert seats(program, TUE) == [] and holds(program, TUE) == (0, 0)
        assert seats(program, WED) == [("Лебедева", "9:10", "")]

    def test_evenly_between_instructors(self, patient):
        board_days.ensure_boards()
        first, second = patient("1", start_date=TUE), patient("2", start_date=TUE)
        assert {seats(first, WED)[0][0], seats(second, WED)[0][0]} == {"Лебедева", "Соколов"}

    def test_nine_admitted_today(self, patient):
        """9 поступивших сегодня: в сегодняшней шахматке и её блоках их нет вовсе; завтра —
        автоматически, по одному в ячейку. В среду работают Лебедева и Соколов, у каждого 4
        дневных слота: 8 мест (вечер — только для Мото-Л), девятый — «Не распределены» на завтра."""
        board_days.ensure_boards()
        programs = [patient(str(room), start_date=TUE) for room in range(1, 10)]

        assert all(seats(p, TUE) == [] and holds(p, TUE) == (0, 0) for p in programs)
        placed = [p for p in programs if seats(p, WED)]
        assert len(placed) == 8
        assert [holds(p, WED) for p in programs if p not in placed] == [(1, 0)]
        cells = {seats(p, WED)[0][:2] for p in placed}
        assert len(cells) == 8  # автоматически — один пациент в ячейке

    def test_added_today_is_unplaced_today(self, patient):
        """Сегодняшняя шахматка не меняется сама: недостающее — в «Не распределены»."""
        board_days.ensure_boards()
        program = patient()
        assert seats(program, TUE) == [] and holds(program, TUE) == (1, 0)
        assert len(seats(program, WED)) == 1

    def test_first_board_places_everyone(self, patient):
        program = patient()  # шахматок ещё нет — первая раскладывает всех (FR-SCH-13)
        assert len(seats(program, TUE)) == 1 and len(seats(program, WED)) == 1

    def test_moto_l_in_evening_of_two_two(self, make_program, doctor, procedures, staff):
        """Мото-Л и индивидуальное приходят из листа вместе: занятие — вечером у 2/2."""
        board_days.ensure_boards()
        program = make_program(start_date=TUE)
        moto = Procedure.objects.create(
            name="Мото-Л", card_label="Мото-Л", kind=ProcedureKind.CARD_ONLY,
            evening_individual=True,
        )  # fmt: skip
        add_prescription(doctor, Prescription(program=program, procedure=moto))
        add_prescription(doctor, Prescription(program=program, procedure=procedures["individual"]))
        assert seats(program, WED) == [("Лебедева", "18:00", "Мото-Л")]


class TestCarry:
    def test_next_day_copies_cells_and_drops_discharged(self, patient, today, rehab, staff, slots):
        board_days.ensure_boards()
        stays = patient("1", start_date=MON)
        leaves = patient("2", shrm=3, start_date=date(2026, 9, 28))  # выписка 08.10
        for program in (stays, leaves):
            booking = Booking.objects.get(program=program, date=WED)
            board.move_patient(rehab, booking, booking.version, staff["sokolov"], slots["10:30"])

        today["day"] = WED
        board_days.ensure_boards()

        assert seats(stays, THU) == [("Соколов", "10:30", "")]
        assert seats(leaves, THU) == [] and holds(leaves, THU) == (0, 0)

    def test_partner_continues_and_absent_instructor_sends_to_unplaced(
        self, patient, rehab, staff, slots, today
    ):
        board_days.ensure_boards()
        pair, single = patient("1"), patient("2")
        board.place_patient(rehab, pair, staff["volkov"], slots["9:50"], TUE)
        board.place_patient(rehab, single, staff["sokolov"], slots["9:50"], TUE)
        ShiftException.objects.create(instructor=staff["sokolov"], date=THU, is_working=False)
        BoardDay.objects.filter(date=WED).delete()
        Booking.objects.filter(date=WED).delete()
        BoardPatient.objects.filter(date=WED).delete()

        board_days.create_board(WED)
        today["day"] = WED
        board_days.ensure_boards()

        assert seats(pair, WED) == [("Лебедева", "9:50", "")]
        assert seats(single, THU) == [] and holds(single, THU) == (1, 0)


class TestManualEdit:
    def test_several_patients_in_one_cell(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        first, second = patient("1"), patient("2")
        for program in (first, second):
            board.place_patient(rehab, program, staff["sokolov"], slots["11:10"], TUE)
        assert seats(first, TUE) == seats(second, TUE) == [("Соколов", "11:10", "")]
        assert holds(first, TUE) == (0, 0), "занятие взято из «Не распределены»"

    def test_remove_goes_to_cancelled_today_and_tomorrow(self, patient, rehab, today):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        booking = Booking.objects.get(program=program, date=WED)
        today["day"] = WED
        board_days.ensure_boards()

        board.remove_patient(rehab, booking, booking.version)

        assert seats(program, WED) == [] and holds(program, WED) == (0, 1)
        assert seats(program, THU) == [] and holds(program, THU) == (0, 1)
        board.place_patient(rehab, program, Instructor.objects.get(short_name="Соколов"),
                            InstructorSlot.objects.get(start=time(9, 50)), WED)  # fmt: skip
        assert holds(program, WED) == (0, 0)
        assert seats(program, THU) == [("Соколов", "9:50", "")], "возврат тоже переходит"

    def test_tomorrow_edited_separately_is_kept(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        tomorrow = Booking.objects.get(program=program, date=WED)
        board.move_patient(rehab, tomorrow, tomorrow.version, staff["lebedeva"], slots["10:30"])

        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)

        assert seats(program, WED) == [("Лебедева", "10:30", "")]

    def test_only_today_and_tomorrow(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=date(2026, 10, 1))
        with pytest.raises(board.BoardError, match="сегодняшнюю и завтрашнюю"):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], MON)

    def test_doctor_cannot_edit(self, patient, doctor, staff, slots):
        board_days.ensure_boards()
        with pytest.raises(board.BoardError):
            board.place_patient(doctor, patient(), staff["sokolov"], slots["9:10"], TUE)

    def test_busy_slot_is_rejected(self, patient, rehab, staff, slots, procedures):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 50))
        add_prescription(rehab.__class__.objects.get(username="doctor"),
                         Prescription(program=program, procedure=procedures["group"]))  # fmt: skip
        with pytest.raises(board.BoardError, match="уже"):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)


class TestPrescriptionChanges:
    def test_more_lessons_tomorrow_automatically(self, patient, doctor):
        board_days.ensure_boards()
        program = patient(start_date=TUE)
        item = program.prescriptions.get()
        item.per_day = 2
        update_prescription(doctor, item)
        assert len(seats(program, WED)) == 2

    def test_cancelled_individual_leaves_board_without_blocks(self, patient, doctor, rehab):
        program = patient(start_date=MON)  # первая шахматка раскладывает и сегодня
        booking = Booking.objects.get(program=program, date=TUE)
        board.remove_patient(rehab, booking, booking.version)
        item = program.prescriptions.get()
        item.cancel_date = WED
        update_prescription(doctor, item)

        assert holds(program, TUE) == (0, 1)
        assert seats(program, WED) == [] and holds(program, WED) == (0, 0)

    def test_new_group_pushes_individual_to_unplaced(self, patient, doctor, procedures):
        board_days.ensure_boards()
        program = patient(start_date=TUE)
        assert seats(program, WED) == [("Лебедева", "9:10", "")]
        GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))

        add_prescription(doctor, Prescription(program=program, procedure=procedures["group"]))

        assert program.bookings.filter(date=WED, kind=BookingKind.LFK_GROUP).exists()
        assert seats(program, WED) == [] and holds(program, WED) == (1, 0)


class TestStaffChange:
    def test_not_working_sends_patients_to_unplaced(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        board.place_patient(rehab, program, staff["sokolov"], slots["10:30"], TUE)

        staff_changes.set_not_working(rehab, staff["sokolov"], TUE)

        assert seats(program, TUE) == [] and holds(program, TUE) == (1, 0)


class TestScreen:
    def test_board_shows_blocks(self, client, rehab, patient):
        program = patient(start_date=MON)
        booking = Booking.objects.get(program=program, date=TUE)
        board.remove_patient(rehab, booking, booking.version)
        client.force_login(rehab)

        page = client.get(reverse("scheduling:board"), {"date": "2026-10-06"}).content.decode()

        assert "Отменены" in page and "9п Пациентов9" in page

    def test_weekend_and_not_composed(self, client, rehab, db):
        client.force_login(rehab)
        page = client.get(reverse("scheduling:board"), {"date": "2026-10-03"}).content.decode()
        assert "не составляется" in page
        page = client.get(reverse("scheduling:board"), {"date": "2026-09-28"}).content.decode()
        assert "не составлялась" in page

    def test_no_future_beyond_tomorrow(self, client, rehab, db, today):
        """Вперёд — только до завтрашней шахматки: будущих нет, листать туда нечего."""
        client.force_login(rehab)
        url = reverse("scheduling:board")

        far = client.get(url, {"date": "2026-10-20"})
        assert far.context["board"].day == WED
        assert 'max="2026-10-07"' in far.content.decode()

        tomorrow = client.get(url, {"date": "2026-10-07"}).context["board"]
        assert not tomorrow.has_next and tomorrow.following_label == "Завтра"
        assert client.get(url).context["board"].has_next

        today["day"] = date(2026, 10, 9)  # пятница: следующая шахматка — понедельник
        friday = client.get(url).context["board"]
        assert friday.following == date(2026, 10, 12)
        assert friday.following_label == "Понедельник"
        assert "Понедельник" in client.get(url).content.decode()

    def test_public_board_same_limit(self, client, db, settings):
        settings.PUBLIC_BOARD_NETWORKS = []
        page = client.get(reverse("public_board"), {"date": "2026-10-20"})
        assert page.context["board"].day == WED and "Завтра" in page.content.decode()

    def test_cell_panel_lists_every_patient(self, client, rehab, patient, staff, slots):
        board_days.ensure_boards()
        for room in ("1", "2"):
            board.place_patient(rehab, patient(room), staff["sokolov"], slots["9:50"], TUE)
        client.force_login(rehab)

        page = client.get(
            reverse("scheduling:board_cell"),
            {"date": "2026-10-06", "instructor": staff["sokolov"].pk, "slot": slots["9:50"].pk},
        ).content.decode()

        assert "1п Пациентов1" in page and "2п Пациентов2" in page
        assert "Поставить ещё пациента" in page
        assert page.count("Убрать из сетки") == 2


class TestCellList:
    """Список пациентов по клику на свободное время (FR-SCH-10)."""

    def test_unplaced_then_cancelled_then_others(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        other, cancelled, unplaced = patient("1", start_date=MON), patient("2"), patient("3")
        board.place_patient(rehab, cancelled, staff["sokolov"], slots["9:10"], TUE)
        booking = Booking.objects.get(program=cancelled, date=TUE)
        board.remove_patient(rehab, booking, booking.version)
        board.place_patient(rehab, other, staff["sokolov"], slots["10:30"], TUE)

        result = board.programs_for_cell(rehab, TUE)

        assert [(c.program.room, c.group) for c in result] == [
            ("3", board.UNPLACED),
            ("2", board.CANCELLED),
            ("1", board.OTHER),
        ]
        assert result[2].where == "10:30 Соколов"
        assert unplaced.room in result[0].search

    def test_single_placement_is_moved_here(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient()
        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)

        board.place_patient(rehab, program, staff["volkov"], slots["11:10"], TUE)

        assert seats(program, TUE) == [("Волков", "11:10", "")]

    def test_two_placements_need_choice(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(per_day=2)
        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)
        board.place_patient(rehab, program, staff["sokolov"], slots["10:30"], TUE)
        with pytest.raises(board.BoardError, match="уже 2 индивидуальных"):
            board.place_patient(rehab, program, staff["volkov"], slots["11:10"], TUE)

    def test_instructor_off_is_rejected(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        with pytest.raises(board.BoardError, match="не работает"):
            board.place_patient(rehab, patient(), staff["lebedeva"], slots["9:10"], TUE)

    def test_note_moves_to_tomorrow(self, patient, rehab, staff, slots):
        board_days.ensure_boards()
        program = patient(start_date=MON)
        board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)
        booking = Booking.objects.get(program=program, date=TUE)

        board.set_note(rehab, booking, booking.version, "  art  ")

        assert seats(program, TUE) == [("Соколов", "9:50", "art")]
        assert seats(program, WED) == [("Соколов", "9:50", "art")]


class TestDragAndPanels:
    def test_drag_patient_to_other_cell(self, client, rehab, patient, staff, slots):
        board_days.ensure_boards()
        program = patient()
        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)
        booking = Booking.objects.get(program=program, date=TUE)
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:board_move", args=[booking.pk]),
            {
                "date": "2026-10-06",
                "version": booking.version,
                "target": f"{staff['volkov'].pk}:{slots['10:30'].pk}",
            },
        )

        assert "Пациент перенесён" in response.content.decode()
        assert seats(program, TUE) == [("Волков", "10:30", "")]

    def test_drag_from_block_with_error_shows_panel(self, client, rehab, patient, staff, slots):
        board_days.ensure_boards()
        program = patient()
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:board_place"),
            {
                "date": "2026-10-06",
                "program": program.pk,
                "target": f"{staff['lebedeva'].pk}:{slots['9:10'].pk}",
            },
        )

        assert response.status_code == 200
        assert "не работает" in response.content.decode()
        assert holds(program, TUE) == (1, 0)

    def test_patient_panel_from_block(self, client, rehab, patient, staff):
        board_days.ensure_boards()
        program = patient()
        client.force_login(rehab)

        page = client.get(
            reverse("scheduling:board_patient"), {"date": "2026-10-06", "program": program.pk}
        ).content.decode()

        assert "Не распределены" in page and "Соколов" in page
        assert (
            client.get(
                reverse("scheduling:board_patient"), {"date": "2026-10-06", "program": 999999}
            ).status_code
            == 404
        )

    def test_board_is_draggable_for_rehab_only(self, client, rehab, doctor, patient):
        program = patient(start_date=MON)
        client.force_login(rehab)
        page = client.get(reverse("scheduling:board"), {"date": "2026-10-06"}).content.decode()
        assert "data-drop" in page and "js/board.js" in page
        assert f'data-program="{program.pk}"' not in page, "пациент в сетке, а не в блоке"

        client.force_login(doctor)
        page = client.get(reverse("scheduling:board"), {"date": "2026-10-06"}).content.decode()
        assert "data-drop" not in page


def test_individuals_after_last_board_are_dropped(patient, staff, slots):
    program = patient(start_date=MON)
    later = Booking.objects.filter(program=program, date=WED).get()
    Booking.objects.create(
        program=program, prescription=later.prescription, procedure=later.procedure,
        kind=BookingKind.INDIVIDUAL, date=date(2026, 10, 14), start=time(9, 10),
        end=time(9, 40), instructor=staff["sokolov"], slot=slots["9:10"],
    )  # fmt: skip

    assert board_days.drop_beyond_boards() == 1
    assert len(seats(program, WED)) == 1, "шахматки не тронуты"


class TestTrainersYield:
    """Тренажёр, поставленный подбором, уступает место индивидуальному: сдвигается в своём
    окне. Окно st-150 в тестах — 9:00–10:00 (старты 9:00, 9:15, 9:30, 9:45), слот 9:10–9:40."""

    @pytest.fixture
    def trainee(self, patient, doctor, procedures):
        Equipment.objects.filter(name="st-150").update(
            window_start=time(9, 0), window_end=time(10, 0)
        )

        def make(room: str = "9"):
            program = patient(room)
            add_prescription(
                doctor, Prescription(program=program, procedure=procedures["equipment"])
            )
            return program

        return make

    def trainer(self, program, day):
        return Booking.objects.get(program=program, date=day, kind=BookingKind.EQUIPMENT)

    def test_tomorrow_auto_seat_moves_trainer(self, trainee):
        board_days.ensure_boards()
        program = trainee()

        assert seats(program, WED) == [("Лебедева", "9:10", "")]
        assert self.trainer(program, WED).start == time(9, 45)
        assert holds(program, WED) == (0, 0)

    def test_manual_place_moves_trainer_and_carries(self, trainee, rehab, staff, slots):
        board_days.ensure_boards()
        program = trainee()
        assert self.trainer(program, TUE).start == time(9, 0)

        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)

        assert self.trainer(program, TUE).start == time(9, 45)
        assert seats(program, WED) == [("Соколов", "9:10", "")]
        assert holds(program, WED) == (0, 0)

    def test_pinned_trainer_does_not_move(self, trainee, rehab, staff, slots):
        board_days.ensure_boards()
        program = trainee()
        today = self.trainer(program, TUE)
        manual.edit_booking(rehab, today, today.version, manual.Target(start=time(9, 15)))

        # Закреплённый вручную тренажёр в 9:15 не сдвигается: ячейка 9:10 невозможна.
        with pytest.raises(board.BoardError, match="st-150"):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)
        assert self.trainer(program, TUE).start == time(9, 15)

    def test_no_room_keeps_rule(self, trainee, rehab, staff, slots):
        board_days.ensure_boards()
        Equipment.objects.filter(name="st-150").update(window_end=time(9, 30))
        program = trainee()  # старты 9:00 и 9:15 — оба на 9:10–9:40

        with pytest.raises(board.BoardError, match="st-150"):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], TUE)
        assert self.trainer(program, TUE).start == time(9, 0)  # откат: тренажёр на месте

    def test_trainer_takes_time_freed_by_old_seat(self, trainee, rehab, staff, slots):
        """Завтра пациент переезжает с 9:10 на 9:50 (за сегодняшней правкой): тренажёр уходит
        с 9:45 на 9:30 — на время старого места, которое освобождается в той же правке."""
        Equipment.objects.filter(name="st-150").update(window_end=time(10, 30))
        board_days.ensure_boards()
        program = trainee()
        assert seats(program, WED) == [("Лебедева", "9:10", "")]
        assert self.trainer(program, WED).start == time(9, 45)

        board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)

        assert seats(program, WED) == [("Соколов", "9:50", "")]
        assert self.trainer(program, WED).start == time(9, 30)
