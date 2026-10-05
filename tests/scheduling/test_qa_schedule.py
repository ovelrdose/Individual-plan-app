"""QA шага 2.1: автоматическое расписание групп ЛФК, бассейна и тренажёров.

TZ.md §7.1, §7.3 (шаги 1, 2, 4), FR-SCH-6, FR-SCH-11/12, FR-CRD-2, §13 пп. 28–31.
Сценарии, которых нет в test_engine.py / test_services.py / test_typical_day.py.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from io import BytesIO
from itertools import pairwise

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Role
from apps.cards.models import CardExport
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling.domain import IssueCode, Need, Snapshot, propose
from apps.scheduling.domain.model import Kind
from apps.scheduling.models import Booking
from apps.scheduling.services import program_schedule, replan, replan_by
from tests.sheets import make_sheet

ALL_GROUPS = (
    "Эрго общая",  # 9:10
    "Вестибулярная гимнастика",  # 9:50
    "I can нога",  # 10:30
    "Нейро-тренинг",  # 10:30, 14:20
    "Лого-тренинг",  # 11:20
    "Эрготерапия",  # 13:40
    "Логоритмика",  # 13:40
    "I can рука",  # 15:00
    "Эрго кисть",  # 15:40
)
EQUIPMENT = ("st-150", "Имитрон", "Pablo")
START = date(2026, 10, 5)


# --- Помощники ---------------------------------------------------------------------------


def proc(name: str) -> Procedure:
    return Procedure.objects.get(name=name, department=None)


def prescribe(user, program: Program, name: str, **fields) -> Prescription:
    return programs.add_prescription(
        user, Prescription(program=program, procedure=proc(name), **fields)
    )


def starts(program: Program, name: str) -> set[time]:
    return {b.start for b in program.bookings.filter(procedure__name=name)}


def issue_codes(program: Program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


def assert_no_patient_overlaps(program: Program) -> None:
    by_day: dict[date, list[tuple[time, time]]] = defaultdict(list)
    for booking in program.bookings.all():
        by_day[booking.date].append((booking.start, booking.end))
    for day, items in by_day.items():
        items.sort()
        for (_, end), (start, _) in pairwise(items):
            assert end <= start, f"{day}: пересечение занятий пациента {items}"


def max_on_equipment(name: str) -> int:
    counts = Counter(
        Booking.objects.filter(equipment__name=name).values_list("date", "start", "end")
    )
    return max(counts.values(), default=0)


def card_sheet(client, program: Program):
    response = client.get(reverse("cards:download", args=[program.pk]))
    assert response.status_code == 200, response
    return load_workbook(BytesIO(response.content)).active


@pytest.fixture
def program(make_program):
    return make_program(shrm=4)  # 05.10–19.10, 15 дней


@pytest.fixture
def other_doctor(make_user, other_department):
    return make_user("doctor2", (other_department, Role.DOCTOR), short_name="Сидорова В.В.")


pytestmark = pytest.mark.usefixtures("full_catalog")


# --- Много назначений сразу ----------------------------------------------------------------


class TestManyPrescriptions:
    def test_all_groups_pool_and_equipment_never_overlap(self, client, doctor, rehab, program):
        for name in ALL_GROUPS:
            prescribe(doctor, program, name)
        prescribe(doctor, program, "ЛФК в воде: нижняя конечность")
        for name in EQUIPMENT:
            prescribe(doctor, program, name)

        assert_no_patient_overlaps(program)
        # Логоритмика 13:40 совпадает с Эрготерапией 13:40 — других занятий нет.
        assert starts(program, "Логоритмика") == set()
        # Бассейн «нижняя»: 9:00 и 9:45 пересекаются с группами 9:10 и 9:50 (реальные
        # интервалы), 14:15 — с Нейро-тренингом 14:20, 15:40 — с Эрго кистью.
        assert starts(program, "ЛФК в воде: нижняя конечность") == set()
        # В окне 13:00–15:00 у пациента свободны только 13:00 и 13:15 — третий тренажёр
        # поставить некуда.
        assert starts(program, "st-150") == {time(13, 0)}
        assert starts(program, "Имитрон") == {time(13, 15)}
        assert starts(program, "Pablo") == set()
        codes = issue_codes(program)
        assert codes.count("GROUP_OVERLAP") == 2
        assert codes.count("EQUIPMENT_UNPLACED") == 1
        assert program.has_schedule_conflicts

        client.force_login(rehab)
        page = client.get(reverse("programs:detail", args=[program.pk]))
        assert page.status_code == 200
        assert "Логоритмика: 05.10" in page.content.decode()

    def test_full_day_fills_the_slot_grid(self, client, doctor, program):
        for name in ALL_GROUPS:
            prescribe(doctor, program, name)
        for name in EQUIPMENT:
            prescribe(doctor, program, name)
        assert len(program_schedule(program).typical) == 10
        client.force_login(doctor)

        sheet = card_sheet(client, program)

        times = [sheet[f"B{row}"].value for row in range(4, 14)]
        assert all(isinstance(value, time) for value in times)
        assert times == sorted(times)
        # Лого-тренинг 11:20 — в строке слота 11:10; два тренажёра — одна строка слота 13:00;
        # 16:20 — окно.
        assert (sheet["B7"].value, sheet["C7"].value) == (time(11, 20), "Лого-тренинг")
        assert (sheet["B8"].value, sheet["C8"].value) == (time(13, 0), "st-150/Имитрон 13:15")
        assert (sheet["B13"].value, sheet["C13"].value) == (time(16, 20), None)

    def test_more_than_10_typical_rows_fold_into_grid(self, client, doctor, program):
        # Без групп 13:40 в окне тренажёров больше места: 7 групп + бассейн + 3 строки
        # тренажёров = 11 строк типичного дня — в карте они делят 10 строк сетки.
        for name in ALL_GROUPS:
            if name not in ("Эрготерапия", "Логоритмика"):
                prescribe(doctor, program, name)
        prescribe(doctor, program, "ЛФК в воде: спина")
        prescribe(doctor, program, "st-150", per_day=2)
        prescribe(doctor, program, "Имитрон")
        prescribe(doctor, program, "Pablo")
        assert_no_patient_overlaps(program)
        assert starts(program, "ЛФК в воде: спина") == {time(13, 30)}
        schedule = program_schedule(program)
        assert len(schedule.typical) == 11
        assert len(schedule.day) == 10
        client.force_login(doctor)

        card_sheet(client, program)
        assert CardExport.objects.filter(program=program).exists()

    def test_more_day_slots_than_card_rows_is_a_clear_error_not_500(self, client, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        InstructorSlot.objects.create(start=time(12, 0), end=time(12, 30))
        client.force_login(doctor)

        response = client.get(reverse("cards:download", args=[program.pk]), follow=True)

        assert response.redirect_chain[-1][0] == reverse("programs:detail", args=[program.pk])
        assert "В расписании 11 строк" in response.content.decode()
        assert not CardExport.objects.filter(program=program).exists()


# --- Группы и бассейн ------------------------------------------------------------------------


class TestGroupsAndPool:
    def test_group_twice_a_day_on_page_and_card(self, client, doctor, rehab, program):
        # Решение владельца 04.10: «2 р/д» под списком групп — каждая группа дважды.
        prescribe(doctor, program, "Нейро-тренинг", per_day=2)
        prescribe(doctor, program, "Эрго общая", per_day=2)

        assert starts(program, "Нейро-тренинг") == {time(10, 30), time(14, 20)}
        assert program.bookings.filter(procedure__name="Нейро-тренинг").count() == 2 * 15
        program.refresh_from_db()
        assert issue_codes(program) == ["GROUP_FREQUENCY"]
        assert "Группа Эрго общая: назначено 2 р/д" in program.schedule_issues[0]["message"]

        client.force_login(rehab)
        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "в расписании групп 1 занятие в день" in html

        sheet = card_sheet(client, program)
        assert (sheet["B6"].value, sheet["C6"].value) == (time(10, 30), "Нейротренинг")
        assert (sheet["B10"].value, sheet["C10"].value) == (time(14, 20), "Нейротренинг")

    def test_pool_and_group_compare_real_intervals(self, doctor, program):
        # Бассейн добавлен первым, но группы ставятся раньше бассейна (§7.3, шаги 1–2).
        prescribe(doctor, program, "ЛФК в воде: нижняя конечность")
        prescribe(doctor, program, "Эрго общая")  # 9:10–9:40 закрывает бассейн 9:00–9:30
        prescribe(doctor, program, "Вестибулярная гимнастика")  # 9:50 закрывает 9:45–10:15

        assert starts(program, "Вестибулярная гимнастика") == {time(9, 50)}
        assert starts(program, "ЛФК в воде: нижняя конечность") == {time(14, 15)}
        assert {b.end for b in program.bookings.filter(kind="POOL")} == {time(14, 45)}
        assert issue_codes(program) == []

    def test_group_order_does_not_matter(self, doctor, program):
        prescribe(doctor, program, "Нейро-тренинг")
        prescribe(doctor, program, "I can нога")

        assert starts(program, "I can нога") == {time(10, 30)}
        assert starts(program, "Нейро-тренинг") == {time(14, 20)}
        assert issue_codes(program) == []

    def test_deleting_blocking_group_returns_other_to_first_session(self, doctor, program):
        blocker = prescribe(doctor, program, "I can нога")
        prescribe(doctor, program, "Нейро-тренинг")
        assert starts(program, "Нейро-тренинг") == {time(14, 20)}

        programs.delete_prescription(doctor, blocker)

        assert starts(program, "Нейро-тренинг") == {time(10, 30)}

    def test_inactive_session_is_not_used(self, doctor, program):
        GroupSession.objects.filter(
            procedure__name="Нейро-тренинг", start_time=time(10, 30)
        ).update(is_active=False)

        prescribe(doctor, program, "Нейро-тренинг")

        assert starts(program, "Нейро-тренинг") == {time(14, 20)}

    def test_group_without_active_sessions_is_a_conflict(self, doctor, program):
        GroupSession.objects.filter(procedure__name="Эрго кисть").update(is_active=False)

        prescribe(doctor, program, "Эрго кисть")

        assert not program.bookings.exists()
        assert issue_codes(program) == ["NO_SESSIONS"]

    def test_session_deactivated_after_planning_moves_on_replan(self, rehab, doctor, program):
        prescribe(doctor, program, "Нейро-тренинг")
        assert starts(program, "Нейро-тренинг") == {time(10, 30)}
        GroupSession.objects.filter(
            procedure__name="Нейро-тренинг", start_time=time(10, 30)
        ).update(is_active=False)

        replan_by(rehab, program)

        assert starts(program, "Нейро-тренинг") == {time(14, 20)}

    def test_changed_session_time_and_duration_on_replan(self, client, rehab, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        GroupSession.objects.filter(procedure__name="Эрго общая").update(
            start_time=time(8, 30), duration_min=45
        )
        client.force_login(rehab)

        response = client.post(reverse("scheduling:program_replan", args=[program.pk]), follow=True)

        assert "Расписание подобрано заново." in response.content.decode()
        assert {(b.start, b.end) for b in program.bookings.all()} == {(time(8, 30), time(9, 15))}

    def test_pool_without_group_message_on_replan(self, client, rehab, doctor, program):
        prescribe(doctor, program, "Бассейн")
        client.force_login(rehab)

        response = client.post(reverse("scheduling:program_replan", args=[program.pk]), follow=True)

        assert "есть конфликты" in response.content.decode()
        assert issue_codes(program) == ["POOL_TYPE_REQUIRED"]


# --- Тренажёры -------------------------------------------------------------------------------


class TestEquipment:
    @pytest.mark.parametrize("per_day", [2, 3])
    def test_several_times_a_day_get_different_times(self, doctor, program, per_day):
        prescribe(doctor, program, "st-150", per_day=per_day)

        assert program.bookings.count() == 15 * per_day
        assert len(starts(program, "st-150")) == per_day
        assert_no_patient_overlaps(program)
        assert len(program_schedule(program).typical) == per_day

    def test_per_day_more_than_window_is_a_conflict_not_500(self, doctor, program):
        prescribe(doctor, program, "st-150", per_day=9)  # в окне 8 начал

        assert program.bookings.count() == 15 * 8
        assert "EQUIPMENT_UNPLACED" in issue_codes(program)

    def test_per_day_change_follows(self, doctor, program):
        item = prescribe(doctor, program, "Имитрон")
        item.per_day = 2

        programs.update_prescription(doctor, item)

        assert program.bookings.count() == 30

    def test_ten_programs_capacity_one(self, client, doctor, make_program):
        Equipment.objects.filter(name="st-150").update(capacity=1)
        made = [
            make_program(full_name=f"Пациент{n} П.П.", history_number=str(n)) for n in range(10)
        ]
        for item in made:
            prescribe(doctor, item, "st-150")

        assert max_on_equipment("st-150") == 1
        # §13 п. 31: восемь пациентов расходятся по восьми началам окна.
        assert len({b.start for b in Booking.objects.filter(equipment__name="st-150")}) == 8
        for late in made[8:]:
            assert not late.bookings.exists()
            assert issue_codes(late) == ["EQUIPMENT_UNPLACED"]
        client.force_login(doctor)
        html = client.get(reverse("programs:list")).content.decode()
        assert html.count("конфликты расписания") == 2

    def test_ten_programs_capacity_two_spread_and_never_over(self, doctor, make_program):
        Equipment.objects.filter(name="st-150").update(capacity=2)
        made = [
            make_program(full_name=f"Пациент{n} П.П.", history_number=str(n)) for n in range(10)
        ]
        for item in made:
            prescribe(doctor, item, "st-150")

        assert max_on_equipment("st-150") == 2
        assert all(item.bookings.count() == 15 for item in made)
        assert len({next(iter(starts(item, "st-150"))) for item in made[:8]}) == 8

    def test_other_department_program_occupies_equipment(
        self, doctor, other_doctor, other_department, program
    ):
        Equipment.objects.filter(name="st-150").update(capacity=1)
        foreign = programs.save_program(
            other_doctor,
            Program(
                department=other_department,
                full_name="Чужой Ч.Ч.",
                room="1",
                shrm=4,
                attending_doctor=other_doctor,
                start_date=START,
            ),
            end_date_changed=False,
        )
        prescribe(other_doctor, foreign, "st-150")

        prescribe(doctor, program, "st-150")

        assert starts(foreign, "st-150") == {time(13, 0)}
        assert starts(program, "st-150") == {time(13, 15)}

    def test_changed_window_step_and_duration(self, rehab, doctor, program):
        prescribe(doctor, program, "Pablo")
        Equipment.objects.filter(name="Pablo").update(
            window_start=time(14, 0), window_end=time(15, 0), step_min=20, duration_min=20
        )

        replan_by(rehab, program)

        assert {(b.start, b.end) for b in program.bookings.all()} == {(time(14, 0), time(14, 20))}

    def test_long_duration_twice_a_day_does_not_overlap_itself(self, doctor, program):
        Equipment.objects.filter(name="st-150").update(duration_min=30)

        prescribe(doctor, program, "st-150", per_day=2)

        assert starts(program, "st-150") == {time(13, 0), time(13, 30)}
        assert_no_patient_overlaps(program)

    def test_inactive_equipment_is_not_booked(self, doctor, program):
        Equipment.objects.filter(name="st-150").update(is_active=False)

        prescribe(doctor, program, "st-150")

        assert not program.bookings.exists()
        assert "EQUIPMENT_UNPLACED" in issue_codes(program)


def test_engine_equipment_without_starts_reports_conflict():
    course = tuple(START + timedelta(days=i) for i in range(3))
    need = Need(1, 1, Kind.EQUIPMENT, "st-150", course, equipment_id=1, starts=(), duration=15)

    proposal = propose(Snapshot(needs=(need,)))

    assert [issue.code for issue in proposal.conflicts] == [IssueCode.EQUIPMENT_UNPLACED]


# --- Курс и назначения: расписание следует за изменениями -------------------------------------


class TestCourseFollows:
    @pytest.mark.parametrize(("shrm", "days"), [(3, 11), (4, 15), (5, 20)])
    def test_course_length_by_shrm(self, doctor, make_program, shrm, days):
        item = make_program(shrm=shrm)
        prescribe(doctor, item, "Эрго общая")
        prescribe(doctor, item, "st-150")

        group_days = sorted(item.bookings.filter(kind="LFK_GROUP").values_list("date", flat=True))
        assert group_days == item.course_dates()
        assert len(group_days) == days
        assert item.bookings.filter(kind="EQUIPMENT").count() == days

    def test_extended_course_keeps_time_on_new_dates(self, doctor, program):
        prescribe(doctor, program, "Нейро-тренинг")
        prescribe(doctor, program, "st-150")
        program.end_date = date(2026, 10, 22)

        programs.save_program(doctor, program, end_date_changed=True)

        days = sorted(program.bookings.filter(kind="LFK_GROUP").values_list("date", flat=True))
        assert days[-1] == date(2026, 10, 22) and len(days) == 18
        assert starts(program, "Нейро-тренинг") == {time(10, 30)}
        assert starts(program, "st-150") == {time(13, 0)}

    def test_shrm_change_recalculates_course_and_schedule(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        program.shrm = 3

        programs.save_program(doctor, program, end_date_changed=False)

        assert program.bookings.count() == 11

    def test_course_start_shift(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        program.start_date = date(2026, 10, 7)

        programs.save_program(doctor, program, end_date_changed=False)

        days = sorted(program.bookings.values_list("date", flat=True))
        assert days == program.course_dates()
        assert days[0] == date(2026, 10, 7)

    def test_prescription_start_moved_later(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        item.start_date = date(2026, 10, 10)

        programs.update_prescription(doctor, item)

        assert min(program.bookings.values_list("date", flat=True)) == date(2026, 10, 10)
        assert program.bookings.count() == 10

    def test_cancel_removed_restores_bookings(self, doctor, program):
        item = prescribe(doctor, program, "st-150", cancel_date=date(2026, 10, 8))
        assert program.bookings.count() == 3
        item.cancel_date = None

        programs.update_prescription(doctor, item)

        assert program.bookings.count() == 15

    def test_cancel_on_last_day_excludes_last_day(self, doctor, program):
        prescribe(doctor, program, "Эрго общая", cancel_date=date(2026, 10, 19))

        assert max(program.bookings.values_list("date", flat=True)) == date(2026, 10, 18)

    def test_procedure_change_moves_bookings(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        item.procedure = proc("I can нога")

        programs.update_prescription(doctor, item)

        assert starts(program, "Эрго общая") == set()
        assert starts(program, "I can нога") == {time(10, 30)}

    def test_procedure_changed_to_card_only_removes_bookings(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        item.procedure = proc("Алмаг")

        programs.update_prescription(doctor, item)

        assert not program.bookings.exists()
        assert issue_codes(program) == []


# --- Закреплённые занятия ----------------------------------------------------------------------


def pin_and_move(program: Program, name: str, day: date, start: time, end: time) -> Booking:
    booking = program.bookings.get(procedure__name=name, date=day)
    Booking.objects.filter(pk=booking.pk).update(pinned=True, start=start, end=end)
    return booking


DEVIATION_DAY = date(2026, 10, 7)


def make_deviation(program: Program) -> None:
    """Эрго общая + Нейро-тренинг; закреплённая вручную Эрго закрывает 10:30 07.10 и 14:20
    08.10. Ни одно время Нейро-тренинга не свободно во все дни → 10:30 с заменой на 07.10."""
    program.bookings.filter(procedure__name="Нейро-тренинг", date=DEVIATION_DAY).delete()
    pin_and_move(program, "Эрго общая", DEVIATION_DAY, time(10, 30), time(11))
    pin_and_move(program, "Эрго общая", date(2026, 10, 8), time(14, 20), time(14, 50))
    replan(program)


class TestPinned:
    def test_pinned_blocks_group_on_its_day_only(self, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        pinned = pin_and_move(program, "Эрго общая", date(2026, 10, 6), time(10, 30), time(11))

        prescribe(doctor, program, "I can нога")

        booked = set(program.bookings.filter(procedure__name="I can нога").values_list("date"))
        assert (date(2026, 10, 6),) not in booked and len(booked) == 14
        program.refresh_from_db()
        (conflict,) = program.schedule_issues
        assert conflict["code"] == "GROUP_OVERLAP" and "06.10" in conflict["message"]
        assert Booking.objects.get(pk=pinned.pk).start == time(10, 30)

    def test_deviation_day_typical_day_page_and_card(self, client, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, "Нейро-тренинг")
        day = DEVIATION_DAY

        make_deviation(program)

        assert program.bookings.get(procedure__name="Нейро-тренинг", date=day).start == time(14, 20)
        program.refresh_from_db()
        assert [i["code"] for i in program.schedule_issues] == ["DAY_DEVIATION"]
        assert "07.10 — в 14:20 вместо 10:30" in program.schedule_issues[0]["message"]
        schedule = program_schedule(program)
        assert [(r.label, r.start, r.deviations) for r in schedule.typical] == [
            ("Группа Эрго общая", 9 * 60 + 10, (day, date(2026, 10, 8))),
            ("Нейротренинг", 10 * 60 + 30, (day,)),
        ]
        row = next(r for r in schedule.calendar if r.date == day)
        assert all(cell.deviation for cell in row.cells)

        client.force_login(rehab)
        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "07.10 — в 14:20 вместо 10:30" in html
        assert "+1 иначе" in html

        sheet = card_sheet(client, program)  # с отклонениями карта выгружается (FR-CRD-3)
        assert [sheet[f"B{r}"].value for r in (4, 5, 6)] == [time(9, 10), time(9, 50), time(10, 30)]
        assert [sheet[f"C{r}"].value for r in (4, 5, 6)] == [
            "Группа Эрго общая",
            None,
            "Нейротренинг",
        ]

    def test_pinned_one_of_two_daily_units_keeps_second(self, doctor, program):
        prescribe(doctor, program, "st-150", per_day=2)
        day = date(2026, 10, 6)
        booking = program.bookings.get(date=day, start=time(13, 0))
        Booking.objects.filter(pk=booking.pk).update(pinned=True)

        replan(program)

        assert program.bookings.filter(date=day).count() == 2

    def test_pinned_of_replaced_procedure_is_removed(self, doctor, program):
        item = prescribe(doctor, program, "Эрго общая")
        booking = program.bookings.get(date=date(2026, 10, 6))
        Booking.objects.filter(pk=booking.pk).update(pinned=True)
        item.procedure = proc("I can нога")

        programs.update_prescription(doctor, item)

        assert starts(program, "Эрго общая") == set()


# --- Импорт листа → расписание сразу ------------------------------------------------------


class TestImport:
    def test_import_gets_schedule_immediately(self, doctor, department):
        sheet = parse_sheet(
            make_sheet(
                rows=[
                    (
                        "05.10.26",
                        "Групповые занятия ЛФК\n- Эрго общая\n- I can нога\n30 мин 1 р/д е/д",
                        "",
                    ),
                    ("05.10.26", "ST – 150 15 мин 1 р/д е/д", ""),
                    ("05.10.26", "Имитрон 15 мин 2 р/д е/д", ""),
                    ("05.10.26", "Алмаг 20 мин 1 р/д е/д", ""),
                ]
            )
        )

        program = import_prescription_sheet(doctor, department, sheet, attending_doctor=doctor)

        assert program.start_date == START
        assert starts(program, "Эрго общая") == {time(9, 10)}
        assert starts(program, "I can нога") == {time(10, 30)}
        assert starts(program, "st-150") == {time(13, 0)}
        assert starts(program, "Имитрон") == {time(13, 15), time(13, 30)}
        assert program.bookings.count() == 15 * 5
        assert issue_codes(program) == []
        assert_no_patient_overlaps(program)


# --- Карта: верхний блок -----------------------------------------------------------------


class TestCard:
    @pytest.mark.parametrize(("shrm", "date_cell"), [(3, "C17"), (4, "C17"), (5, "C18")])
    def test_top_block_by_time_with_excel_values(
        self, client, doctor, make_program, shrm, date_cell
    ):
        item = make_program(shrm=shrm)
        # Порядок назначений другой, чем порядок по времени.
        prescribe(doctor, item, "st-150")
        prescribe(doctor, item, "Лого-тренинг")  # место пустое
        prescribe(doctor, item, "Нейро-тренинг")
        prescribe(doctor, item, "Эрго общая")
        client.force_login(doctor)

        sheet = card_sheet(client, item)

        rows = [
            (sheet[f"B{r}"].value, sheet[f"C{r}"].value, sheet[f"F{r}"].value) for r in range(4, 10)
        ]
        # Строки — слоты сетки; 9:50 и 13:40 — окна пациента.
        assert rows == [
            (time(9, 10), "Группа Эрго общая", "зал ЛФК"),
            (time(9, 50), None, None),
            (time(10, 30), "Нейротренинг", "эргозона"),
            (time(11, 20), "Лого-тренинг", None),
            (time(13, 0), "st-150", "зона БОС терапии"),
            (time(13, 40), None, None),
        ]
        assert sheet["B4"].number_format == "h:mm"
        first_date = sheet[date_cell].value
        assert isinstance(first_date, datetime) and first_date.date() == START
        assert sheet[date_cell].number_format == "dd.mm"

    def test_pool_row_uses_pool_place(self, client, doctor, program):
        prescribe(doctor, program, "ЛФК в воде: спина")
        client.force_login(doctor)

        sheet = card_sheet(client, program)

        assert (sheet["B4"].value, sheet["C4"].value) == (time(9, 10), None)
        assert (sheet["B6"].value, sheet["C6"].value, sheet["F6"].value) == (
            time(10, 30),
            "ЛФК в воде",
            "бассейн",
        )

    def test_extended_course_prints_on_bigger_template(self, client, doctor, program):
        # Решение владельца 04.10: продлённый ШРМ 4 (18 дней) печатается на шаблоне ШРМ 5.
        prescribe(doctor, program, "Эрго общая")
        program.end_date = date(2026, 10, 22)
        programs.save_program(doctor, program, end_date_changed=True)
        client.force_login(doctor)

        response = client.get(reverse("cards:download", args=[program.pk]))

        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/vnd.openxmlformats")


# --- Права на «Подобрать заново» и экраны ---------------------------------------------------


class TestRights:
    def url(self, program: Program) -> str:
        return reverse("scheduling:program_replan", args=[program.pk])

    def test_admin_can_replan(self, client, admin_user, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        client.force_login(admin_user)

        response = client.post(self.url(program))

        assert response.status_code == 302
        assert response.url == reverse("programs:detail", args=[program.pk])

    def test_rehab_of_other_department_gets_404(self, client, make_user, other_department, program):
        stranger = make_user("rehab2", (other_department, Role.REHAB))
        client.force_login(stranger)

        assert client.post(self.url(program)).status_code == 404

    def test_anonymous_is_sent_to_login(self, client, doctor, program):
        prescribe(doctor, program, "Эрго общая")
        GroupSession.objects.filter(procedure__name="Эрго общая").update(start_time=time(8, 0))

        response = client.post(self.url(program))

        assert response.status_code == 302 and "login" in response.url
        assert starts(program, "Эрго общая") == {time(9, 10)}

    def test_doctor_cannot_replan_through_service(self, doctor, program):
        with pytest.raises(PermissionDenied):
            replan_by(doctor, program)

    def test_missing_program_is_404(self, client, rehab):
        client.force_login(rehab)

        assert client.post(reverse("scheduling:program_replan", args=[999999])).status_code == 404

    def test_admin_sees_replan_button(self, client, admin_user, program):
        client.force_login(admin_user)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "Подобрать заново" in html
        assert "ставить нечего" in html


class TestScreens:
    def test_list_badges(self, client, doctor, make_program):
        card_only = make_program(full_name="Строкин С.С.", history_number="1")
        deviation = make_program(full_name="Отклонов О.О.", history_number="2")
        prescribe(doctor, card_only, "Алмаг")
        prescribe(doctor, deviation, "Эрго общая")
        prescribe(doctor, deviation, "Нейро-тренинг")
        make_deviation(deviation)
        assert issue_codes(deviation) == ["DAY_DEVIATION"]
        client.force_login(doctor)

        html = client.get(reverse("programs:list")).content.decode()

        rows = {
            name: html.split(name, 1)[1].split("</tr>", 1)[0]
            for name in ("Строкин С.С.", "Отклонов О.О.")
        }
        assert "расписание" not in rows["Строкин С.С."]
        assert "расписание составлено" in rows["Отклонов О.О."]

    def test_htmx_add_refreshes_schedule_block(self, client, doctor, program):
        client.force_login(doctor)

        response = client.post(
            reverse("programs:prescription_add", args=[program.pk]),
            {"procedure": proc("Эрго общая").pk, "per_day": "1", "in_card": "on"},
            HTTP_HX_REQUEST="true",
        )

        assert program.bookings.count() == 15
        assert 'id="schedule"' in response.content.decode()
