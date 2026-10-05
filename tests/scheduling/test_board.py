"""Шахматка на дату (TZ.md §7.6, FR-SCH-14…16): просмотр, правка ячеек, права.

Справочник — полный (load_initial_catalog), инструкторы — вымышленные из seed_dev: одиночки 5/2
Соколов, Морозова, Зайцев, Орлова, Кузнецова; пары 2/2 Волков/ Лебедева, Голубев/ Белова.
Соколов и Морозова 15:00–16:50 — «Метод. работа», Орлова — ведение групп и БОС.
"""

from datetime import date, time, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Role
from apps.catalog.models import InstructorSlot, Procedure
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling import board
from apps.scheduling.domain.validate import ViolationCode
from apps.scheduling.models import Booking, BookingSource, RemovedSession
from apps.scheduling.services import replan
from apps.staff.models import Instructor, InstructorBlock

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
    return program


def slot(hour: int, minute: int = 0) -> InstructorSlot:
    return InstructorSlot.objects.get(start=time(hour, minute))


def individual(program: Program, day: date = MONDAY) -> Booking:
    return program.bookings.get(kind="INDIVIDUAL", date=day)


def seat_text(result: board.Board, instructor: str, start: time) -> str:
    for column in result.columns:
        for cell in column.cells:
            if cell.slot.start != start:
                continue
            for seat in cell.seats:
                if seat.instructor.short_name == instructor:
                    return seat.patient or (seat.busy.text if seat.busy else "")
    raise AssertionError(f"нет ячейки {instructor} {start}")


# --- Просмотр -------------------------------------------------------------------------------


class TestView:
    def test_columns_are_teams_on_shift(self, rehab, patient):
        result = board.board(rehab, MONDAY)

        labels = [column.label for column in result.columns]
        assert "Волков/ Лебедева" in labels and "Голубев/ Белова" in labels
        assert {"Соколов", "Морозова", "Зайцев", "Орлова", "Кузнецова"} <= set(labels)

    def test_weekend_has_only_pairs(self, rehab, patient):
        labels = [column.label for column in board.board(rehab, SATURDAY).columns]

        assert labels == ["Волков/ Лебедева", "Голубев/ Белова"]

    def test_pair_cell_shows_member_on_shift(self, rehab, patient, staff):
        column = next(
            c for c in board.board(rehab, MONDAY).columns if c.label == "Волков/ Лебедева"
        )

        assert len(column.instructors) == 1, "в будний день работает один из пары"
        assert all(len(cell.seats) <= 1 for cell in column.cells)

    def test_patient_and_duties_in_cells(self, rehab, patient):
        booking = individual(patient)
        result = board.board(rehab, MONDAY)

        assert seat_text(result, booking.instructor.short_name, booking.start) == "9п Иванов"
        assert seat_text(result, "Соколов", time(15)) == "Метод. работа"
        assert seat_text(result, "Орлова", time(13)) == "БОС"
        assert seat_text(result, "Орлова", time(9, 10)) == "Эрго общая"

    def test_other_department_patient_label_without_link(
        self, make_user, other_department, staff, patient, department
    ):
        other_doctor = make_user("doc1", (other_department, Role.DOCTOR))
        other = programs.save_program(
            other_doctor,
            Program(
                department=other_department,
                full_name="Петров Пётр Петрович",
                room="3",
                shrm=4,
                attending_doctor=other_doctor,
                start_date=MONDAY,
            ),
            end_date_changed=False,
        )
        prescribe(other_doctor, other, INDIVIDUAL)
        viewer = make_user("viewer", (department, Role.REHAB))

        result = board.board(viewer, MONDAY)

        seats = [s for c in result.columns for cell in c.cells for s in cell.seats if s.booking]
        theirs = next(s for s in seats if s.booking.program_id == other.pk)
        ours = next(s for s in seats if s.booking.program_id == patient.pk)
        assert theirs.patient == "3п ОМР № 1 Петров" and theirs.program_id is None
        assert ours.patient == "9п Иванов" and ours.program_id == patient.pk

    def test_weekend_individuals_listed_without_instructor(self, rehab, patient):
        result = board.board(rehab, SATURDAY)

        start = individual(patient).start
        assert result.weekend_individuals == [(start, "9п Иванов", patient.pk)]

    def test_equipment_table(self, doctor, rehab, patient):
        prescribe(doctor, patient, "st-150")

        result = board.board(rehab, MONDAY)

        names = [item.name for item in result.equipment]
        column = names.index("st-150")
        start = patient.bookings.get(kind="EQUIPMENT", date=MONDAY).start
        row = next(r for r in result.equipment_rows if r.start == start)
        assert row.cells[column] == (["9п Иванов"], False)
        assert row.in_window

    def test_page_for_doctor_is_read_only(self, client, doctor, patient):
        client.force_login(doctor)

        html = client.get(reverse("scheduling:board"), {"date": "2026-10-05"}).content.decode()

        assert "9п Иванов" in html
        assert "board_cell" not in html and "/schedule/board/cell/" not in html
        assert (
            client.get(
                reverse("scheduling:board_cell"),
                {"date": "2026-10-05", "instructor": individual(patient).instructor_id, "slot": 1},
            ).status_code
            == 403
        )

    def test_page_for_rehab_has_edit_buttons_and_navigation(self, client, rehab, patient):
        client.force_login(rehab)

        html = client.get(reverse("scheduling:board"), {"date": "2026-10-05"}).content.decode()

        assert "/schedule/board/cell/?date=2026-10-05" in html
        assert "?date=2026-10-04" in html and "?date=2026-10-06" in html

    def test_bad_date_falls_back_to_today(self, client, rehab, staff):
        client.force_login(rehab)

        assert client.get(reverse("scheduling:board"), {"date": "31.02"}).status_code == 200


# --- Пациент в ячейке -----------------------------------------------------------------------


def free_seat(day: date, *, exclude_instructor: int | None = None, start: time | None = None):
    for instructor, item in board.free_seats(day):
        if exclude_instructor is not None and instructor.pk == exclude_instructor:
            continue
        if start is not None and item.start != start:
            continue
        if item.is_evening:
            continue
        return instructor, item
    raise AssertionError("нет свободной ячейки")


class TestPlace:
    def test_place_moves_auto_booking_and_pins_it(self, rehab, patient):
        before = individual(patient)
        instructor, target = free_seat(MONDAY, exclude_instructor=before.instructor_id)

        booking = board.place_patient(rehab, patient, instructor, target, MONDAY)

        assert booking.pk == before.pk, "занятие подбора переносится, а не добавляется"
        after = individual(patient)
        assert (after.instructor, after.slot, after.start) == (instructor, target, target.start)
        assert after.pinned and after.source == BookingSource.MANUAL
        assert after.version == before.version + 1
        # Пересборка закреплённое не трогает, остальные даты — на месте.
        replan(patient)
        assert individual(patient).instructor == instructor
        assert patient.bookings.filter(kind="INDIVIDUAL").count() == 15

    def test_busy_cell_is_refused(self, rehab, patient, staff):
        with pytest.raises(board.BoardError) as error:
            board.place_patient(rehab, patient, staff["Соколов"], slot(15), MONDAY)

        assert "Метод. работа" in str(error.value)
        assert error.value.violations[0].code == ViolationCode.INSTRUCTOR_BUSY

    def test_cell_with_other_patient_is_refused(self, doctor, rehab, patient, make_program):
        other = make_program(full_name="Сидоров Сидор Сидорович", room="4")
        prescribe(doctor, other, INDIVIDUAL)
        taken = individual(other)

        with pytest.raises(board.BoardError) as error:
            board.place_patient(rehab, patient, taken.instructor, taken.slot, MONDAY)

        assert "уже пациент 4п Сидоров" in str(error.value)

    def test_patient_overlap_with_group_is_refused(self, doctor, rehab, patient, staff):
        prescribe(doctor, patient, "Эрго общая")  # 9:10–9:40
        group = patient.bookings.get(kind="LFK_GROUP", date=MONDAY)
        instructor, target = free_seat(MONDAY, start=group.start)

        with pytest.raises(board.BoardError) as error:
            board.place_patient(rehab, patient, instructor, target, MONDAY)

        assert error.value.violations[0].code == ViolationCode.PATIENT_OVERLAP

    def test_adjacent_individual_is_refused(self, doctor, rehab, make_program, staff):
        program = make_program(full_name="Двоев Дмитрий Дмитриевич", room="6")
        prescribe(doctor, program, INDIVIDUAL, per_day=2)
        first, second = program.bookings.filter(kind="INDIVIDUAL", date=MONDAY).order_by("start")
        # Второе занятие вплотную после первого (перерыв 10 минут).
        neighbour = InstructorSlot.objects.filter(start__gt=first.end).order_by("start").first()
        instructor, target = free_seat(MONDAY, start=neighbour.start)

        with pytest.raises(board.BoardError) as error:
            board.move_patient(rehab, second, second.version, instructor, target)

        assert error.value.violations[0].code == ViolationCode.INDIVIDUAL_ADJACENT

    def test_extra_individual_needs_reason(self, rehab, patient):
        first = individual(patient)
        instructor, target = free_seat(MONDAY, exclude_instructor=first.instructor_id)
        board.place_patient(rehab, patient, instructor, target, MONDAY)  # перенос, закреплено
        far = next(
            (i, s)
            for i, s in board.free_seats(MONDAY)
            if not s.is_evening and abs(s.start.hour - target.start.hour) >= 2
        )

        with pytest.raises(board.ConfirmationRequired) as error:
            board.place_patient(rehab, patient, *far, MONDAY)
        assert error.value.violations[0].code == ViolationCode.INDIVIDUAL_LIMIT

        board.place_patient(rehab, patient, *far, MONDAY, reason="по просьбе врача")
        extra = patient.bookings.filter(kind="INDIVIDUAL", date=MONDAY)
        assert extra.count() == 2
        latest = extra.get(slot=far[1]).history.first()
        assert latest.history_change_reason == "по просьбе врача"
        assert latest.history_user == rehab

    def test_weekend_is_refused(self, rehab, patient, staff):
        with pytest.raises(board.BoardError, match="дежурные 2/2"):
            board.place_patient(rehab, patient, staff["Волков"], slot(9, 50), SATURDAY)

    def test_patient_without_individual_is_refused(self, rehab, staff, make_program):
        program = make_program(full_name="Безинд Борис Борисович", room="7")

        with pytest.raises(board.BoardError, match="нет индивидуального"):
            board.place_patient(rehab, program, staff["Зайцев"], slot(9, 50), MONDAY)

    def test_stale_version_is_refused(self, rehab, patient):
        booking = individual(patient)
        instructor, target = free_seat(MONDAY, exclude_instructor=booking.instructor_id)

        with pytest.raises(board.BoardError, match="изменена другим пользователем"):
            board.move_patient(rehab, booking, booking.version + 1, instructor, target)

    def test_evening_slot_by_hand(self, rehab, patient, staff):
        evening = InstructorSlot.objects.filter(is_evening=True).first()
        if evening is None:
            pytest.skip("в сетке нет вечерних слотов")
        instructor, target = next((i, s) for i, s in board.free_seats(MONDAY) if s.pk == evening.pk)

        board.move_patient(
            rehab, individual(patient), individual(patient).version, instructor, target
        )

        assert individual(patient).slot == evening


class TestRemove:
    def test_removed_session_is_not_returned_by_replan(self, doctor, rehab, patient):
        booking = individual(patient)

        board.remove_patient(rehab, booking, booking.version)

        assert not patient.bookings.filter(kind="INDIVIDUAL", date=MONDAY).exists()
        assert RemovedSession.objects.get(prescription=booking.prescription, date=MONDAY).units == 1
        prescribe(doctor, patient, "st-150")  # любая правка назначений пересобирает
        assert not patient.bookings.filter(kind="INDIVIDUAL", date=MONDAY).exists()
        assert patient.bookings.filter(kind="INDIVIDUAL").count() == 14

    def test_placing_again_undoes_removal(self, rehab, patient):
        booking = individual(patient)
        board.remove_patient(rehab, booking, booking.version)
        instructor, target = free_seat(MONDAY)

        board.place_patient(rehab, patient, instructor, target, MONDAY)

        assert not RemovedSession.objects.exists()
        assert individual(patient).pinned
        replan(patient)
        assert patient.bookings.filter(kind="INDIVIDUAL", date=MONDAY).count() == 1


class TestRights:
    def test_doctor_cannot_edit(self, doctor, patient, staff):
        booking = individual(patient)

        with pytest.raises(board.BoardError, match="специалист ФР"):
            board.remove_patient(doctor, booking, booking.version)
        with pytest.raises(board.BoardError, match="специалист ФР"):
            board.add_block(doctor, staff["Зайцев"], slot(9, 50), MONDAY, "METHOD_WORK")

    def test_rehab_of_other_department_cannot_touch_our_patient(
        self, make_user, other_department, patient, staff
    ):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        booking = individual(patient)

        with pytest.raises(board.BoardError, match="другого отделения"):
            board.remove_patient(stranger, booking, booking.version)
        # Блоки — ресурс центра: специалист любого отделения.
        board.add_block(stranger, staff["Зайцев"], slot(9, 50), MONDAY, "BOS")

    def test_programs_for_cell_only_own_departments_with_individual(
        self, make_user, other_department, doctor, rehab, patient, make_program
    ):
        no_individual = make_program(full_name="Группов Глеб Глебович", room="2")
        prescribe(doctor, no_individual, "Эрго общая")
        stranger = make_user("stranger", (other_department, Role.REHAB))

        assert [p.pk for p in board.programs_for_cell(rehab, MONDAY)] == [patient.pk]
        assert board.programs_for_cell(stranger, MONDAY) == []
        assert board.programs_for_cell(rehab, MONDAY + timedelta(days=30)) == []


# --- Блоки ----------------------------------------------------------------------------------


class TestBlocks:
    def test_block_range_skips_dates_with_patient(self, rehab, patient):
        booking = individual(patient)
        tuesday = individual(patient, MONDAY + timedelta(days=1))
        board.remove_patient(rehab, tuesday, tuesday.version)

        result = board.add_block(
            rehab,
            booking.instructor,
            booking.slot,
            MONDAY,
            "OTHER",
            "Совещание",
            until=MONDAY + timedelta(days=2),
        )

        assert result.skipped == [MONDAY, MONDAY + timedelta(days=2)]
        assert result.created == [MONDAY + timedelta(days=1)]
        blocks = InstructorBlock.objects.filter(instructor=booking.instructor, slot=booking.slot)
        assert {b.label for b in blocks} == {"Совещание"}

    def test_block_kind_and_label_checked(self, rehab, staff):
        with pytest.raises(board.BoardError, match="вид блока"):
            board.add_block(rehab, staff["Зайцев"], slot(9, 50), MONDAY, "GROUP_LEAD")
        with pytest.raises(board.BoardError, match="чем занят"):
            board.add_block(rehab, staff["Зайцев"], slot(9, 50), MONDAY, "OTHER", " ")
        with pytest.raises(board.BoardError, match="раньше"):
            board.add_block(
                rehab, staff["Зайцев"], slot(9, 50), MONDAY, "BOS", until=MONDAY - timedelta(1)
            )

    def test_block_shows_and_clears(self, rehab, staff, patient):
        board.add_block(rehab, staff["Зайцев"], slot(9, 50), MONDAY, "BOS")
        assert seat_text(board.board(rehab, MONDAY), "Зайцев", time(9, 50)) == "БОС"

        board.clear_block(rehab, staff["Зайцев"], slot(9, 50), MONDAY)

        assert seat_text(board.board(rehab, MONDAY), "Зайцев", time(9, 50)) == ""
        assert not InstructorBlock.objects.exists()

    def test_clear_duty_on_one_date(self, rehab, staff, patient):
        board.clear_block(rehab, staff["Соколов"], slot(15), MONDAY)

        assert seat_text(board.board(rehab, MONDAY), "Соколов", time(15)) == ""
        tuesday = MONDAY + timedelta(days=1)
        assert seat_text(board.board(rehab, tuesday), "Соколов", time(15)) == "Метод. работа"
        # Освободившийся слот можно занять пациентом.
        board.place_patient(rehab, patient, staff["Соколов"], slot(15), MONDAY)

    def test_clear_empty_cell_is_error(self, rehab, staff):
        with pytest.raises(board.BoardError, match="нет блока"):
            board.clear_block(rehab, staff["Зайцев"], slot(9, 50), MONDAY)


# --- Представления HTMX ---------------------------------------------------------------------


class TestViews:
    def post(self, client, name, data, *args):
        return client.post(
            reverse(f"scheduling:{name}", args=args), data, headers={"HX-Request": "true"}
        )

    def test_cell_panel_for_free_cell(self, client, rehab, patient):
        client.force_login(rehab)
        instructor, target = free_seat(MONDAY)

        html = client.get(
            reverse("scheduling:board_cell"),
            {"date": "2026-10-05", "instructor": instructor.pk, "slot": target.pk},
        ).content.decode()

        assert "Поставить пациента" in html and "9п Иванов" in html
        assert "Поставить блок" in html

    def test_place_returns_panel_and_board(self, client, rehab, patient):
        client.force_login(rehab)
        instructor, target = free_seat(MONDAY, exclude_instructor=individual(patient).instructor_id)

        response = self.post(
            client,
            "board_place",
            {
                "date": "2026-10-05",
                "instructor": instructor.pk,
                "slot": target.pk,
                "program": patient.pk,
            },
        )

        html = response.content.decode()
        assert response.status_code == 200
        assert "Пациент поставлен." in html
        assert 'id="board" hx-swap-oob="true"' in html
        assert individual(patient).instructor == instructor

    def test_error_stays_in_panel(self, client, rehab, patient, staff):
        client.force_login(rehab)

        response = self.post(
            client,
            "board_place",
            {"date": "2026-10-05", "instructor": staff["Соколов"].pk, "slot": slot(15).pk,
             "program": patient.pk},
        )  # fmt: skip

        html = response.content.decode()
        assert response.status_code == 200
        assert "Метод. работа" in html and "hx-swap-oob" not in html

    def test_confirmation_asks_for_reason(self, client, rehab, patient):
        first = individual(patient)
        board.place_patient(
            rehab, patient, *free_seat(MONDAY, exclude_instructor=first.instructor_id), MONDAY
        )
        target = next(
            (i, s)
            for i, s in board.free_seats(MONDAY)
            if not s.is_evening and abs(s.start.hour - individual(patient).start.hour) >= 2
        )
        client.force_login(rehab)
        data = {
            "date": "2026-10-05",
            "instructor": target[0].pk,
            "slot": target[1].pk,
            "program": patient.pk,
        }

        html = self.post(client, "board_place", data).content.decode()
        assert 'name="reason"' in html and "а можно 1" in html

        html = self.post(client, "board_place", data | {"reason": "врач"}).content.decode()
        assert "Пациент поставлен." in html

    def test_remove_and_block_actions(self, client, rehab, patient, staff):
        client.force_login(rehab)
        booking = individual(patient)
        cell = {
            "date": "2026-10-05",
            "instructor": booking.instructor_id,
            "slot": booking.slot_id,
        }

        html = self.post(
            client, "board_remove", cell | {"version": booking.version}, booking.pk
        ).content.decode()
        assert "подбор его не вернёт" in html

        html = self.post(client, "board_block", cell | {"kind": "METHOD_WORK"}).content.decode()
        assert "Блок поставлен." in html

        html = self.post(client, "board_block_clear", cell).content.decode()
        assert "Блок убран." in html

    @pytest.mark.parametrize(
        "data",
        [
            {"date": "x", "instructor": "1", "slot": "1"},
            {"date": "2026-10-05", "instructor": "²", "slot": "1"},
            {"date": "2026-10-05", "instructor": "1", "slot": "9" * 30},
        ],
    )
    def test_garbage_is_404(self, client, rehab, staff, data):
        client.force_login(rehab)

        assert self.post(client, "board_block", data | {"kind": "BOS"}).status_code == 404

    def test_doctor_post_gets_error_not_change(self, client, doctor, patient):
        client.force_login(doctor)
        booking = individual(patient)

        response = self.post(
            client,
            "board_remove",
            {"date": "2026-10-05", "instructor": booking.instructor_id,
             "slot": booking.slot_id, "version": booking.version},
            booking.pk,
        )  # fmt: skip

        assert response.status_code == 403
        assert Booking.objects.filter(pk=booking.pk).exists()


class TestPanel:
    def get(self, client, instructor, slot_, day="2026-10-05"):
        return client.get(
            reverse("scheduling:board_cell"),
            {"date": day, "instructor": instructor.pk, "slot": slot_.pk},
        ).content.decode()

    def test_patient_cell_offers_move_and_remove(self, client, rehab, patient):
        client.force_login(rehab)
        booking = individual(patient)

        html = self.get(client, booking.instructor, booking.slot)

        assert "Перенести" in html and "Убрать занятие на эту дату" in html
        assert f'name="version" value="{booking.version}"' in html
        instructor, target = free_seat(MONDAY)
        assert f'value="{instructor.pk}:{target.pk}"' in html

    def test_other_department_patient_is_read_only(
        self, client, make_user, other_department, department, patient
    ):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        client.force_login(stranger)
        booking = individual(patient)

        html = self.get(client, booking.instructor, booking.slot)

        assert "правит специалист ФР этого отделения" in html
        assert "Перенести" not in html

    def test_duty_cell_offers_clear_for_date(self, client, rehab, staff):
        client.force_login(rehab)

        html = self.get(client, staff["Соколов"], slot(15))

        assert "Метод. работа" in html and "постоянный распорядок" in html
        assert "Снять распорядок на 05.10" in html

    def test_weekend_free_cell_offers_only_block(self, client, rehab, staff):
        client.force_login(rehab)
        working = next(c.instructors[0] for c in board.board(rehab, SATURDAY).columns)

        html = self.get(client, working, slot(9, 50), day="2026-10-10")

        assert "В выходные пациентов в шахматку не ставят" in html
        assert "Поставить блок" in html and "Поставить пациента" not in html

    def test_off_shift_instructor(self, client, rehab, staff):
        client.force_login(rehab)

        html = self.get(client, staff["Соколов"], slot(9, 50), day="2026-10-10")

        assert "в этот день не работает" in html
