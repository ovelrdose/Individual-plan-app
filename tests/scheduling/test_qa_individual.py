"""QA шага 3.2: автоматический подбор индивидуальных занятий «как в жизни» (TZ.md §7.3, §7.4,
§8.2, решения 6, 7, 43, 44, 49–55).

Справочник — полный (load_initial_catalog из расписаний групп заказчика), инструкторы — вымышленные
из seed_dev: пять одиночек 5/2 (Соколов, Морозова, Зайцев, Орлова, Кузнецова), две пары 2/2
(Волков/ Лебедева, Голубев/ Белова), распорядок с «Метод. работой», ведением групп и БОС.
Листы назначений — только из tests/sheets.py.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from io import BytesIO, StringIO
from itertools import pairwise

import pytest
from django.core.management import call_command
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Role
from apps.catalog.models import GroupSession, InstructorSlot, Procedure
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling import services
from apps.scheduling.models import Booking
from apps.scheduling.services import replan
from apps.staff.models import Instructor, InstructorDuty, ShiftException, ShiftPattern
from apps.staff.services import instructor_calendars
from tests.sheets import make_sheet

pytestmark = pytest.mark.usefixtures("full_catalog")

START = date(2026, 10, 5)  # понедельник; ШРМ 4 — 05.10–19.10, выходные 10–11 и 17–18.10
WEEKEND = {START + timedelta(days=d) for d in (5, 6, 12, 13)}
INDIVIDUAL = "Индивидуальное занятие"
LUNCH = (time(11, 40), time(13, 0))


# --- Помощники ------------------------------------------------------------------------------


def proc(name: str) -> Procedure:
    return Procedure.objects.get(name=name, department=None)


def prescribe(user, program: Program, name: str, **fields) -> Prescription:
    return programs.add_prescription(
        user, Prescription(program=program, procedure=proc(name), **fields)
    )


def individuals(program: Program):
    """Индивидуальные с инструктором — будни. Выходные (без инструктора, решение 56) — в
    ``weekend_individuals``."""
    return (
        program.bookings.filter(kind="INDIVIDUAL", instructor__isnull=False)
        .select_related("instructor__partner", "slot")
        .order_by("date", "start")
    )


def weekend_individuals(program: Program):
    return program.bookings.filter(kind="INDIVIDUAL", instructor__isnull=True).order_by(
        "date", "start"
    )


def by_date(program: Program) -> dict[date, list[tuple[str, time]]]:
    """Дата → [(инструктор, начало)] индивидуальных занятий программы."""
    result: dict[date, list[tuple[str, time]]] = defaultdict(list)
    for b in individuals(program):
        result[b.date].append((b.instructor.short_name, b.start))
    return dict(result)


def codes(program: Program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


def minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def assert_individuals_valid(*items: Program) -> None:
    """Инварианты подбора для каждой индивидуальной записи: инструктор в смене, слот у него
    свободен по распорядку (не «Метод. работа», не ведение группы, не БОС), слот дневной, время
    совпадает со слотом, у пациента нет пересечений и двух занятий подряд (§7.2, решение 6)."""
    bookings = list(
        Booking.objects.filter(program__in=items).select_related("slot", "instructor", "program")
    )
    if not bookings:
        return
    start = min(b.date for b in bookings)
    end = max(b.date for b in bookings)
    calendars = instructor_calendars(Instructor.objects.all(), start, end)
    by_patient: dict[tuple[int, date], list[Booking]] = defaultdict(list)
    for b in bookings:
        by_patient[(b.program_id, b.date)].append(b)
        if b.kind != "INDIVIDUAL":
            continue
        assert b.slot_id is not None
        if b.instructor_id is None:
            assert b.date.weekday() >= 5, f"без инструктора в будний день {b.date}"
            assert (b.start, b.end) == (b.slot.start, b.slot.end)
            continue
        assert b.date.weekday() < 5, f"инструктор в выходной {b.date}"
        day = calendars[b.instructor_id].day(b.date)
        assert day.working, f"{b.instructor} не в смене {b.date}"
        assert day.is_free(b.slot_id), f"{b.instructor} {b.date} {b.start}: слот с обязанностью"
        assert not b.slot.is_evening, "вечерние слоты — только вручную (FR-CAT-5)"
        assert (b.start, b.end) == (b.slot.start, b.slot.end)
    for (_, day), items_ in by_patient.items():
        items_.sort(key=lambda b: b.start)
        for first, second in pairwise(items_):
            assert first.end <= second.start, f"пересечение у пациента {day}: {first} / {second}"
        own = [b for b in items_ if b.kind == "INDIVIDUAL"]
        for first, second in pairwise(own):
            gap = minutes(second.start) - minutes(first.end)
            assert gap > 10, f"два индивидуальных подряд {day}: {first.start} и {second.start}"


@pytest.fixture
def staff(settings, doctor, rehab, admin_user) -> dict[str, Instructor]:
    """Вымышленные инструкторы seed_dev со сменами и распорядком «как в жизни».

    Пользователи doctor / rehab / admin заводятся раньше: seed_dev их только находит."""
    settings.DEBUG = True
    call_command("seed_dev", stdout=StringIO())
    return {item.short_name: item for item in Instructor.objects.all()}


@pytest.fixture
def program(make_program):
    return make_program(shrm=4, start_date=START)


def make_patients(make_program, doctor, count: int, *, per_day=lambda i: 1, group=True):
    """count программ подряд, как их заводят врачи: группа «Эрго общая» и индивидуальное."""
    result = []
    for i in range(count):
        item = make_program(
            full_name=f"Пациентов{i} Пётр Петрович", history_number=str(7000 + i), room=str(i + 1)
        )
        if group:
            prescribe(doctor, item, "Эрго общая")
        prescribe(doctor, item, INDIVIDUAL, per_day=per_day(i))
        result.append(item)
    return result


def weekdays(program: Program) -> list[date]:
    """Даты курса, на которые подбирается инструктор (решение 56)."""
    return [day for day in program.course_dates() if day.weekday() < 5]


def assert_weekends_same_time(program: Program) -> None:
    """Выходные курса: индивидуальное без инструктора в типичное время будней (решение 56)."""
    weekday_starts = Counter(b.start for b in individuals(program))
    typical = weekday_starts.most_common(1)[0][0]
    weekend = {d for d in program.course_dates() if d.weekday() >= 5}
    seats = {b.date: b.start for b in weekend_individuals(program)}
    assert set(seats) == weekend
    assert set(seats.values()) == {typical}


def pairs_only() -> None:
    """Выключить одиночек 5/2: подбор видит только пары 2/2."""
    Instructor.objects.filter(partner__isnull=True).update(is_active=False)


def working(staff_names: list[str], day: date) -> set[str]:
    items = [Instructor.objects.get(short_name=name) for name in staff_names]
    calendars = instructor_calendars(items, day, day)
    return {item.short_name for item in items if calendars[item.pk].is_working(day)}


# --- Импорт листа назначений -------------------------------------------------------------------


def import_sheet(doctor, department, rows) -> Program:
    sheet = parse_sheet(make_sheet(rows=rows, fio="Импортов Иван Иванович", ib="8801", room="7"))
    return import_prescription_sheet(doctor, department, sheet, attending_doctor=doctor)


GROUPS_ROW = ("05.10.26", "Групповые занятия ЛФК\n- Эрго общая\n- I can нога\n30 мин 2 р/д е/д", "")


class TestImport:
    def test_one_per_day_from_sheet(self, doctor, department, staff):
        program = import_sheet(
            doctor,
            department,
            [GROUPS_ROW, ("05.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", "")],
        )

        days = by_date(program)
        assert sorted(days) == weekdays(program)
        assert_weekends_same_time(program)
        assert all(len(seats) == 1 for seats in days.values())
        # Одно время на весь курс — цель закрепления (решение 7).
        assert len({start for seats in days.values() for _, start in seats}) == 1
        assert_individuals_valid(program)
        assert "INDIVIDUAL_UNPLACED" not in codes(program)

    def test_two_per_day_from_sheet_not_adjacent(self, doctor, department, staff):
        program = import_sheet(
            doctor,
            department,
            [GROUPS_ROW, ("05.10.26", "Индивидуальное занятие ЛФК 30 мин 2 р/д е/д", "")],
        )

        days = by_date(program)
        assert len(days) == 11
        assert all(len(seats) == 2 for seats in days.values())
        assert individuals(program).count() == 22
        assert weekend_individuals(program).count() == 8
        # Два времени на курс, одни и те же каждый день.
        assert len({tuple(start for _, start in seats) for seats in days.values()}) == 1
        assert_individuals_valid(program)
        assert program.has_schedule_conflicts is False

    def test_individual_and_group_and_equipment_do_not_overlap(self, doctor, department, staff):
        program = import_sheet(
            doctor,
            department,
            [
                GROUPS_ROW,
                ("05.10.26", "Индивидуальное занятие ЛФК 30 мин 2 р/д е/д", ""),
                ("05.10.26", "ST – 150 15 мин 1 р/д е/д", ""),
            ],
        )

        assert program.bookings.filter(kind="EQUIPMENT").count() == 15
        assert program.bookings.filter(kind="LFK_GROUP").count() == 30
        assert_individuals_valid(program)


# --- Много программ подряд: равномерность и распорядок ------------------------------------------


class TestManyPrograms:
    def test_no_individual_in_duty_slot_or_off_shift(self, doctor, staff, make_program):
        patients = make_patients(make_program, doctor, 20, per_day=lambda i: 1 + i % 2)

        assert_individuals_valid(*patients)
        # «Метод. работа» Соколова, ведение групп и БОС Орловой — ни одной записи в эти слоты.
        duty = {
            (d.instructor_id, d.slot_id) for d in InstructorDuty.objects.all()
        }  # распорядок seed_dev бессрочный
        taken = set(
            Booking.objects.filter(kind="INDIVIDUAL").values_list("instructor_id", "slot_id")
        )
        assert not duty & taken

    def test_instructor_is_never_double_booked(self, doctor, staff, make_program):
        make_patients(make_program, doctor, 15, per_day=lambda i: 2 if i % 3 == 0 else 1)

        rows = Booking.objects.filter(kind="INDIVIDUAL", instructor__isnull=False).values_list(
            "instructor_id", "date", "slot_id"
        )
        assert len(rows) == len(set(rows))

    def test_three_identical_patients_go_to_different_teams(self, doctor, staff, make_program):
        """Сценарий 4 на справочнике «как в жизни»: следующий пациент — к наименее загруженной
        команде, а не в соседний слот той же команды (решение 56)."""
        patients = make_patients(make_program, doctor, 3, group=False)

        main = []
        for p in patients:
            teams = {b.instructor.team_label for b in individuals(p)}
            assert len(teams) == 1, teams
            main.append(teams.pop())
        assert len(set(main)) == 3, main

    def test_weekday_load_is_even_between_teams(self, doctor, staff, make_program):
        """Поровну на рабочий будний день, выходные не в счёт (решение 56)."""
        make_patients(make_program, doctor, 20)

        per_day: dict[date, Counter] = defaultdict(Counter)
        bookings = Booking.objects.filter(kind="INDIVIDUAL", instructor__isnull=False)
        for b in bookings.select_related("instructor__partner"):
            per_day[b.date][b.instructor.team_label] += 1
        for day, load in per_day.items():
            if day in WEEKEND:
                continue
            teams = {i.team_label for i in staff.values()}
            values = [load.get(team, 0) for team in teams]
            assert max(values) - min(values) <= 2, f"{day:%d.%m}: {dict(load)}"

    def test_weekend_never_overflows(self, doctor, staff, make_program):
        """Выходные ведут дежурные 2/2 без шахматки (решение 56): сколько бы ни было
        пациентов, у каждого выходные стоят в его время, конфликтов на выходные нет."""
        patients = make_patients(make_program, doctor, 20)

        for patient in patients:
            assert "INDIVIDUAL_UNPLACED" not in codes(patient)
            assert_weekends_same_time(patient)
        assert not Booking.objects.filter(
            kind="INDIVIDUAL", date__in=WEEKEND, instructor__isnull=False
        ).exists()


# --- Пара 2/2 --------------------------------------------------------------------------------


class TestPair:
    def test_each_date_the_working_member(self, doctor, staff, program):
        pairs_only()
        prescribe(doctor, program, INDIVIDUAL)

        days = by_date(program)
        assert len(days) == 11
        team = {"Волков", "Лебедева"}
        assert {name for seats in days.values() for name, _ in seats} == team
        for day, seats in days.items():
            assert {name for name, _ in seats} == working(sorted(team), day)
        assert len({start for seats in days.values() for _, start in seats}) == 1
        # Чередование напарников — не отклонение (решение 55).
        assert codes(program) == []

    def test_typical_day_shows_team_and_calendar_shows_member(self, client, doctor, staff, program):
        pairs_only()
        prescribe(doctor, program, INDIVIDUAL)
        client.force_login(doctor)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "<th>Инструктор</th>" in html
        assert "<td>Волков/ Лебедева</td>" in html
        for seats in by_date(program).values():
            name, start = seats[0]
            assert f"{start.hour}:{start.minute:02d} {name}" in html
        assert "иначе</span>" not in html, "чередование пары не подсвечивается"

    def test_partner_sick_replaced_on_that_date_only(self, doctor, rehab, staff, program):
        pairs_only()
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        sick = next(day for day, seats in sorted(before.items()) if seats[0][0] == "Лебедева")
        ShiftException.objects.create(instructor=staff["Лебедева"], date=sick, is_working=False)

        replan(program)

        after = by_date(program)
        assert after[sick][0][0] not in {"Волков", "Лебедева"}
        assert after[sick][0][1] == before[sick][0][1], "замена в том же слоте"
        assert {d: s for d, s in after.items() if d != sick} == {
            d: s for d, s in before.items() if d != sick
        }
        assert "DAY_DEVIATION" in codes(program)
        assert_individuals_valid(program)


# --- Инструктор по желанию пациента ------------------------------------------------------------


class TestPreferred:
    def test_single_5_2_all_weekdays_weekend_not_a_replacement(self, doctor, rehab, staff, program):
        """Одиночка 5/2 по желанию пациента ведёт все будни; выходные — время без инструктора,
        это не замена (решение 56)."""
        prescribe(doctor, program, INDIVIDUAL)

        services.choose_preferred_instructor(rehab, program, staff["Морозова"])

        seats = {seat for day_seats in by_date(program).values() for seat in day_seats}
        assert len(by_date(program)) == 11
        assert {name for name, _ in seats} == {"Морозова"}
        assert len(seats) == 1, "одно время на все будни"
        assert_weekends_same_time(program)
        assert codes(program) == []
        assert_individuals_valid(program)

    def test_pair_member_partner_then_anyone(self, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        # В один из дней Лебедевой она на больничном — тогда «любой» (решение 44).
        lebedeva_day = next(day for day in weekdays(program) if working(["Лебедева"], day))
        ShiftException.objects.create(
            instructor=staff["Лебедева"], date=lebedeva_day, is_working=False, reason="больничный"
        )

        services.choose_preferred_instructor(rehab, program, staff["Волков"])

        for day, seats in by_date(program).items():
            name = seats[0][0]
            if working(["Волков"], day):
                assert name == "Волков"
            elif day == lebedeva_day:
                assert name not in {"Волков", "Лебедева"}
            else:
                assert name == "Лебедева"
        assert set(by_date(program)) == set(weekdays(program)), "занятие не пропадает"
        assert_weekends_same_time(program)
        assert set(codes(program)) == {"PREFERRED_REPLACED"}

    def test_preferred_beats_balance_and_previous(self, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        assert {s[0][0] for s in by_date(program).values()} == {"Соколов"}

        services.choose_preferred_instructor(rehab, program, staff["Кузнецова"])

        assert {s[0][0] for s in by_date(program).values()} == {"Кузнецова"}

    def test_clearing_choice_keeps_schedule_valid(self, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Кузнецова"])

        services.choose_preferred_instructor(rehab, program, None)

        assert set(by_date(program)) == set(weekdays(program))
        assert_weekends_same_time(program)
        assert "PREFERRED_REPLACED" not in codes(program)
        assert_individuals_valid(program)

    def test_preferred_deactivated_later_falls_back_without_error(
        self, doctor, rehab, staff, program
    ):
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Кузнецова"])
        Instructor.objects.filter(pk=staff["Кузнецова"].pk).update(is_active=False)

        replan(program)

        days = by_date(program)
        assert set(days) == set(weekdays(program))
        assert "Кузнецова" not in {s[0][0] for s in days.values()}
        assert set(codes(program)) == {"PREFERRED_REPLACED"}


# --- Устойчивость (решение 49) -------------------------------------------------------------------


class TestStability:
    def test_new_group_moves_only_conflicting_dates(self, doctor, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        start = {s[0][1] for s in before.values()}.pop()
        group = GroupSession.objects.filter(
            procedure__department=None, procedure__kind="LFK_GROUP", start_time=start
        ).first()
        assert group is not None, "в расписании групп есть группа в это время"
        changed_from = START + timedelta(days=7)

        prescribe(doctor, program, group.procedure.name, start_date=changed_from)

        after = by_date(program)
        for day in weekdays(program):
            if day < changed_from:
                assert after[day] == before[day], f"{day} не должен меняться"
            else:
                assert after[day][0][1] != start, f"{day}: группа заняла время"
        assert_individuals_valid(program)

    def test_moved_dates_stay_with_same_team(self, doctor, staff, program):
        pairs_only()
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        assert {s[0][0] for s in before.values()} == {"Волков", "Лебедева"}
        assert {s[0][1] for s in before.values()} == {time(9, 10)}
        changed_from = START + timedelta(days=7)

        # С 12.10 врач добавил «Эрго общая» (9:10) — время индивидуального занято.
        prescribe(doctor, program, "Эрго общая", start_date=changed_from)

        after = by_date(program)
        moved = {d: s for d, s in after.items() if d >= changed_from}
        assert {s[0][0] for s in moved.values()} <= {"Волков", "Лебедева"}, moved
        program.refresh_from_db()
        messages = " ".join(i["message"] for i in program.schedule_issues)
        assert f"{START:%d.%m}" not in messages, "не изменившаяся дата — не отклонение"

    def test_new_duty_mid_course_moves_only_its_dates(self, doctor, staff, program):
        """Специалист ФР поставил Волкову «Метод. работу» в слот пациента с середины курса:
        после пересборки меняются только даты Волкова с этого дня (решение 49)."""
        pairs_only()
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        start = {s[0][1] for s in before.values()}.pop()
        since = START + timedelta(days=6)
        InstructorDuty.objects.create(
            instructor=staff["Волков"],
            slot=InstructorSlot.objects.get(start=start),
            kind="METHOD_WORK",
            valid_from=since,
        )

        prescribe(doctor, program, "st-150")  # любая правка назначений пересобирает

        after = by_date(program)
        moved = {d for d in before if after[d] != before[d]}
        assert moved == {d for d, s in before.items() if d >= since and s[0][0] == "Волков"}
        assert moved, "у Волкова есть даты после начала распорядка"
        assert_individuals_valid(program)

    def test_per_day_changes_keep_existing_seats(self, doctor, staff, program):
        item = prescribe(doctor, program, INDIVIDUAL, per_day=1)
        one = by_date(program)

        item.per_day = 2
        programs.update_prescription(doctor, item)
        two = by_date(program)

        assert all(set(one[d]) <= set(two[d]) and len(two[d]) == 2 for d in one)
        assert_individuals_valid(program)

        item.per_day = 1
        programs.update_prescription(doctor, item)
        back = by_date(program)

        assert all(len(back[d]) == 1 and set(back[d]) <= set(two[d]) for d in two)

    def test_delete_individual_prescription_removes_bookings(self, doctor, staff, program):
        item = prescribe(doctor, program, INDIVIDUAL, per_day=2)
        prescribe(doctor, program, "Эрго общая")

        programs.delete_prescription(doctor, item)

        assert not individuals(program).exists()
        assert program.bookings.filter(kind="LFK_GROUP").count() == 15
        assert codes(program) == []

    def test_new_equipment_does_not_move_individual(self, doctor, staff, program):
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, INDIVIDUAL, per_day=2)
        before = by_date(program)

        prescribe(doctor, program, "st-150")
        prescribe(doctor, program, "Имитрон")

        assert by_date(program) == before
        assert program.bookings.filter(kind="EQUIPMENT").count() == 30
        assert_individuals_valid(program)

    def test_other_programs_do_not_jump_when_one_changes(self, doctor, staff, make_program):
        patients = make_patients(make_program, doctor, 6)
        before = {p.pk: by_date(p) for p in patients}

        prescribe(doctor, patients[2], "Нейро-тренинг")
        prescribe(doctor, patients[2], "st-150")
        programs.delete_prescription(
            doctor, patients[4].prescriptions.get(procedure__name="Эрго общая")
        )

        for p in patients:
            if p == patients[2]:
                continue
            assert by_date(p) == before[p.pk], f"{p.full_name}: занятия «прыгнули»"

    def test_group_schedule_screen_keeps_individual_if_no_conflict(
        self, client, doctor, rehab, staff, make_program
    ):
        today = date.today()
        current = make_program(start_date=today)
        prescribe(doctor, current, "Нейро-тренинг")
        prescribe(doctor, current, INDIVIDUAL)
        before = by_date(current)
        individual_times = {s[0][1] for s in before.values()}
        session = GroupSession.objects.filter(procedure=proc("Нейро-тренинг")).order_by("pk")[0]
        busy = {session.start_time} | individual_times
        new_time = next(
            s.start
            for s in InstructorSlot.objects.filter(is_evening=False).order_by("start")
            if s.start not in busy and s.start > max(individual_times)
        )
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:session_edit", args=[session.pk]),
            {
                "edit-start_time": f"{new_time:%H:%M}",
                "edit-duration_min": session.duration_min,
                "edit-place": session.place,
                "edit-is_active": "on",
            },
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        session.refresh_from_db()
        assert session.start_time == new_time
        assert by_date(current) == before
        assert_individuals_valid(current)

    def test_group_schedule_screen_moves_individual_on_conflict(
        self, client, doctor, rehab, staff, make_program
    ):
        today = date.today()
        current = make_program(start_date=today)
        prescribe(doctor, current, "Нейро-тренинг")
        prescribe(doctor, current, INDIVIDUAL)
        before = by_date(current)
        start = {s[0][1] for s in before.values()}
        assert len(start) == 1
        session = GroupSession.objects.filter(procedure=proc("Нейро-тренинг")).order_by("pk")[0]
        client.force_login(rehab)
        # Все занятия группы переносим на время индивидуального.
        for item in GroupSession.objects.filter(procedure=proc("Нейро-тренинг")):
            response = client.post(
                reverse("scheduling:session_edit", args=[item.pk]),
                {
                    "edit-start_time": f"{start.copy().pop():%H:%M}",
                    "edit-duration_min": 30,
                    "edit-place": item.place,
                    "edit-is_active": "on" if item.pk == session.pk else "",
                },
                headers={"HX-Request": "true"},
            )
            assert response.status_code == 200

        after = by_date(current)
        assert set(after) == set(before)
        assert all(seats[0][1] not in start for seats in after.values())
        group = current.bookings.filter(kind="LFK_GROUP")
        assert set(group.values_list("start", flat=True)) == start
        assert_individuals_valid(current)

    def test_replan_from_scratch_redistributes(
        self, client, doctor, rehab, admin_user, staff, make_program
    ):
        pairs_only()
        first, second = make_patients(make_program, doctor, 2, group=False)
        assert {s[0][0] for s in by_date(second).values()} == {"Голубев", "Белова"}
        # Первую программу удалили — Волков/ Лебедева освободились, но вторая «не прыгает».
        programs.delete_program(admin_user, first)
        replan(second)
        assert {s[0][0] for s in by_date(second).values()} == {"Голубев", "Белова"}
        client.force_login(rehab)

        response = client.post(reverse("scheduling:program_replan", args=[second.pk]))

        assert response.status_code == 302
        assert {s[0][0] for s in by_date(second).values()} == {"Волков", "Лебедева"}
        assert_individuals_valid(second)


# --- Изменения курса (FR-SCH-11, FR-SCH-12) ----------------------------------------------


class TestCourseChanges:
    def test_cancel_removes_from_cancel_date_only(self, doctor, staff, program):
        item = prescribe(doctor, program, INDIVIDUAL, per_day=2)
        before = by_date(program)
        cancel = START + timedelta(days=6)

        item.cancel_date = cancel
        programs.update_prescription(doctor, item)

        after = by_date(program)
        assert max(after) == max(d for d in weekdays(program) if d < cancel)
        assert after == {d: s for d, s in before.items() if d < cancel}
        assert all(b.date < cancel for b in weekend_individuals(program))

    def test_late_start_prescription(self, doctor, staff, program):
        start = START + timedelta(days=3)

        prescribe(doctor, program, INDIVIDUAL, start_date=start)

        assert min(by_date(program)) == start
        assert sorted(by_date(program)) == [d for d in weekdays(program) if d >= start]
        assert {b.date for b in weekend_individuals(program)} == {
            d for d in program.course_dates() if d >= start and d.weekday() >= 5
        }

    def test_shorten_course(self, doctor, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        program.end_date = START + timedelta(days=9)

        programs.save_program(doctor, program, end_date_changed=True)

        after = by_date(program)
        assert max(after) == program.end_date
        assert after == {d: s for d, s in before.items() if d <= program.end_date}

    def test_extend_course_continues_same_time_and_team(self, doctor, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        before = by_date(program)
        program.end_date = START + timedelta(days=17)

        programs.save_program(doctor, program, end_date_changed=True)

        after = by_date(program)
        assert sorted(after) == weekdays(program)
        assert len(after) == 14
        assert {d: s for d, s in after.items() if d in before} == before
        times = {s[0][1] for s in after.values()}
        assert len(times) == 1, "продление — то же время (FR-SCH-12)"
        assert {s[0][0] for s in after.values()} == {s[0][0] for s in before.values()}
        assert_weekends_same_time(program)
        assert_individuals_valid(program)

    @pytest.mark.parametrize(("shrm", "dates", "weekend"), [(3, 11, 2), (4, 15, 4), (5, 20, 5)])
    def test_course_length_by_shrm(self, doctor, staff, make_program, shrm, dates, weekend):
        item = make_program(shrm=shrm)
        prescribe(doctor, item, "Эрго общая")

        prescribe(doctor, item, INDIVIDUAL, per_day=2)

        days = by_date(item)
        assert len(item.course_dates()) == dates
        assert sorted(days) == weekdays(item)
        assert all(len(s) == 2 for s in days.values())
        assert weekend_individuals(item).count() == 2 * weekend
        assert_individuals_valid(item)


# --- Лимит отделения в день (решение 54) --------------------------------------------------------


class TestDepartmentLimit:
    def test_limit_lowered_after_prescription_caps_with_warning(
        self, doctor, staff, department, program
    ):
        prescribe(doctor, program, INDIVIDUAL, per_day=2)
        department.max_individual_per_day = 1
        department.save()

        replan(program)

        assert all(len(s) == 1 for s in by_date(program).values())
        program.refresh_from_db()
        limit = [i for i in program.schedule_issues if i["code"] == "INDIVIDUAL_LIMIT"]
        assert limit and not limit[0]["conflict"]
        assert program.has_schedule_conflicts is False

    def test_limit_is_shared_by_two_prescriptions(self, doctor, staff, program):
        first = prescribe(doctor, program, INDIVIDUAL, per_day=2)
        second = prescribe(doctor, program, INDIVIDUAL, per_day=1)

        assert all(len(s) == 2 for s in by_date(program).values())
        assert first.bookings.count() == 30
        assert second.bookings.count() == 0
        assert "INDIVIDUAL_LIMIT" in codes(program)

    def test_pinned_counts_against_limit(self, doctor, staff, program):
        prescribe(doctor, program, INDIVIDUAL, per_day=1)
        extra = prescribe(doctor, program, INDIVIDUAL, per_day=1)
        pinned = extra.bookings.order_by("date").first()
        pinned.pinned = True
        pinned.save()

        replan(program)

        assert all(len(s) == 2 for s in by_date(program).values())
        assert Booking.objects.filter(pk=pinned.pk).exists()

    def test_service_rejects_per_day_above_limit(self, doctor, staff, department, program):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            prescribe(doctor, program, INDIVIDUAL, per_day=3)


# --- Другое отделение ------------------------------------------------------------------------


class TestOtherDepartment:
    def other_program(self, make_user, other_department) -> tuple:
        other_doctor = make_user("other_doc", (other_department, Role.DOCTOR))
        item = programs.save_program(
            other_doctor,
            Program(
                department=other_department,
                full_name="Чужой Чужак Чужакович",
                room="12",
                shrm=4,
                attending_doctor=other_doctor,
                start_date=START,
            ),
            end_date_changed=False,
        )
        return other_doctor, item

    def test_other_department_load_counts(
        self, doctor, make_user, other_department, staff, program
    ):
        other_doctor, other = self.other_program(make_user, other_department)
        prescribe(other_doctor, other, INDIVIDUAL)
        assert {s[0][0] for s in by_date(other).values()} == {"Соколов"}

        prescribe(doctor, program, INDIVIDUAL)

        ours = {(d, s[0][0], s[0][1]) for d, s in by_date(program).items()}
        theirs = {(d, s[0][0], s[0][1]) for d, s in by_date(other).items()}
        assert not ours & theirs
        # Нагрузка чужого отделения учитывается: идём к другой, незагруженной команде.
        assert "Соколов" not in {name for _, name, _ in ours}

    def test_replan_of_ours_does_not_touch_theirs(
        self, doctor, rehab, make_user, other_department, staff, program
    ):
        other_doctor, other = self.other_program(make_user, other_department)
        prescribe(other_doctor, other, INDIVIDUAL)
        theirs = by_date(other)
        prescribe(doctor, program, INDIVIDUAL)

        services.replan_by(rehab, program)

        assert by_date(other) == theirs


# --- Нет инструкторов ------------------------------------------------------------------------


class TestNoInstructors:
    def test_import_without_instructors_conflict_and_pages_work(self, client, doctor, department):
        program = import_sheet(
            doctor,
            department,
            [GROUPS_ROW, ("05.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", "")],
        )

        program.refresh_from_db()
        unplaced = [i for i in program.schedule_issues if i["code"] == "INDIVIDUAL_UNPLACED"]
        assert unplaced and unplaced[0]["conflict"]
        assert "нет свободного инструктора" in unplaced[0]["message"]
        assert f"{START:%d.%m}" in unplaced[0]["message"]
        assert program.bookings.filter(kind="LFK_GROUP").count() == 30, "группы поставлены"
        client.force_login(doctor)
        detail = client.get(reverse("programs:detail", args=[program.pk]))
        assert detail.status_code == 200
        assert "нет свободного инструктора" in detail.content.decode()
        card = client.get(reverse("cards:download", args=[program.pk]))
        assert card.status_code == 200

    def test_instructors_without_shift_patterns(self, doctor, staff, program):
        ShiftPattern.objects.all().delete()

        prescribe(doctor, program, INDIVIDUAL)

        assert not individuals(program).exists()
        assert "INDIVIDUAL_UNPLACED" in codes(program)

    def test_all_instructors_deactivated(self, client, doctor, staff, make_program):
        current = make_program(start_date=date.today())
        Instructor.objects.update(is_active=False)

        prescribe(doctor, current, INDIVIDUAL)

        assert "INDIVIDUAL_UNPLACED" in codes(current)
        client.force_login(doctor)
        html = client.get(reverse("programs:list")).content.decode()
        assert "конфликты расписания" in html


# --- Карта (§8.2) ------------------------------------------------------------------------------


def card_sheet(client, user, program: Program):
    client.force_login(user)
    response = client.get(reverse("cards:download", args=[program.pk]))
    assert response.status_code == 200
    return load_workbook(BytesIO(response.content)).active


class TestCard:
    @pytest.mark.parametrize(("shrm", "dates_cell"), [(3, "C17"), (4, "C17"), (5, "C18")])
    def test_individual_rows_on_their_slots_and_windows(
        self, client, doctor, staff, make_program, shrm, dates_cell
    ):
        program = make_program(shrm=shrm)
        prescribe(doctor, program, "Эрго общая")
        prescribe(doctor, program, INDIVIDUAL, per_day=2)
        prescribe(doctor, program, "st-150")
        individual_times = {start for s in by_date(program).values() for _, start in s}
        assert len(individual_times) == 2

        sheet = card_sheet(client, doctor, program)

        grid = [s.start for s in InstructorSlot.objects.filter(is_evening=False).order_by("start")]
        rows = {sheet[f"B{r}"].value: r for r in range(4, 14) if sheet[f"B{r}"].value}
        assert all(isinstance(v, time) for v in rows), "время — значения Excel time"
        assert all(sheet[f"B{r}"].number_format == "h:mm" for r in rows.values())
        for start in individual_times:
            r = rows[start]
            assert sheet[f"C{r}"].value == "Инд.занятие"
            assert not sheet[f"F{r}"].value, "место индивидуального — пусто"
        assert sheet[f"C{rows[time(9, 10)]}"].value == "Группа Эрго общая"
        busy = individual_times | {time(9, 10)}
        windows = [t for t in grid if t not in busy and t not in (time(13, 0), time(13, 40))]
        assert windows, "окна остаются"
        for start in windows:
            if start in rows:
                assert sheet[f"C{rows[start]}"].value is None, f"окно {start}"
        first = sheet[dates_cell]
        assert isinstance(first.value, datetime)
        assert first.value.date() == program.start_date
        assert first.number_format.lower() in {"dd.mm", "dd.mm;@"}

    def test_card_lists_individual_once_in_lower_block(self, client, doctor, staff, program):
        prescribe(doctor, program, INDIVIDUAL, per_day=2)

        sheet = card_sheet(client, doctor, program)

        lower = [sheet[f"A{r}"].value for r in range(18, 34)]
        assert lower.count("Инд.занятие") == 1

    def test_card_after_preferred_change(self, client, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Соколов"])
        weekday = {s[0][1] for d, s in by_date(program).items() if d not in WEEKEND}

        sheet = card_sheet(client, doctor, program)

        labels = {sheet[f"B{r}"].value: sheet[f"C{r}"].value for r in range(4, 14)}
        assert labels[weekday.pop()] == "Инд.занятие"


# --- Страница программы и права на выбор инструктора -------------------------------------


class TestPreferredView:
    def url(self, program: Program) -> str:
        return reverse("scheduling:program_preferred_instructor", args=[program.pk])

    def test_admin_can_choose(self, client, doctor, admin_user, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        client.force_login(admin_user)

        response = client.post(
            self.url(program), {"instructor": staff["Зайцев"].pk}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 200
        program.refresh_from_db()
        assert program.preferred_instructor == staff["Зайцев"]

    def test_anonymous_is_redirected_to_login(self, client, staff, program):
        response = client.post(self.url(program), {"instructor": staff["Зайцев"].pk})

        assert response.status_code == 302
        assert "login" in response.url
        program.refresh_from_db()
        assert program.preferred_instructor is None

    def test_rehab_of_other_department_gets_404_on_page_and_action(
        self, client, make_user, other_department, staff, program
    ):
        client.force_login(make_user("stranger", (other_department, Role.REHAB)))

        assert client.get(reverse("programs:detail", args=[program.pk])).status_code == 404
        assert client.post(self.url(program), {"instructor": ""}).status_code == 404

    def test_doctor_403_keeps_choice(self, client, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Зайцев"])
        client.force_login(doctor)

        assert client.post(self.url(program), {"instructor": ""}).status_code == 403
        program.refresh_from_db()
        assert program.preferred_instructor == staff["Зайцев"]

    def test_unknown_instructor_is_error_in_fragment(self, client, doctor, rehab, staff, program):
        """Неизвестный номер — та же ошибка, что и для мусора в поле: выбор не меняется."""
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Зайцев"])
        client.force_login(rehab)

        response = client.post(
            self.url(program), {"instructor": "999999"}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 200
        assert "Выберите инструктора из списка." in response.content.decode()
        program.refresh_from_db()
        assert program.preferred_instructor == staff["Зайцев"]

    def test_inactive_instructor_error_in_fragment(self, client, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        Instructor.objects.filter(pk=staff["Зайцев"].pk).update(is_active=False)
        client.force_login(rehab)

        response = client.post(
            self.url(program), {"instructor": staff["Зайцев"].pk}, headers={"HX-Request": "true"}
        )

        assert response.status_code == 200
        assert "Выберите действующего инструктора." in response.content.decode()
        program.refresh_from_db()
        assert program.preferred_instructor is None

    @pytest.mark.parametrize("value", ["abc", "-1", "1.5", " ", "1; DROP TABLE", "9" * 30])
    def test_garbage_values_do_not_500(self, client, doctor, rehab, staff, program, value):
        prescribe(doctor, program, INDIVIDUAL)
        client.force_login(rehab)

        response = client.post(
            self.url(program), {"instructor": value}, headers={"HX-Request": "true"}
        )

        assert response.status_code in (200, 400, 404)

    def test_superscript_digit_does_not_500(self, client, doctor, rehab, staff, program):
        client.force_login(rehab)
        client.raise_request_exception = False

        response = client.post(self.url(program), {"instructor": "²"})

        assert response.status_code != 500

    def test_superscript_digit_in_pool_group_does_not_500(
        self, client, doctor, rehab, staff, program
    ):
        item = prescribe(doctor, program, "Эрго общая")
        client.force_login(rehab)
        client.raise_request_exception = False

        response = client.post(
            reverse("scheduling:program_pool_group", args=[program.pk, item.pk]), {"group": "²"}
        )

        assert response.status_code != 500

    def test_garbage_value_does_not_clear_choice(self, client, doctor, rehab, staff, program):
        prescribe(doctor, program, INDIVIDUAL)
        services.choose_preferred_instructor(rehab, program, staff["Зайцев"])
        client.force_login(rehab)

        client.post(self.url(program), {"instructor": "abc"}, headers={"HX-Request": "true"})

        program.refresh_from_db()
        assert program.preferred_instructor == staff["Зайцев"]

    def test_select_lists_active_instructors_pairs_together(
        self, client, doctor, rehab, staff, program
    ):
        prescribe(doctor, program, INDIVIDUAL)
        Instructor.objects.filter(pk=staff["Морозова"].pk).update(is_active=False)
        client.force_login(rehab)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert 'name="instructor"' in html
        assert f'value="{staff["Морозова"].pk}"' not in html
        volkov = html.index(f'value="{staff["Волков"].pk}"')
        lebedeva = html.index(f'value="{staff["Лебедева"].pk}"')
        assert html.index(">Зайцев<") > lebedeva > volkov, "пара рядом, порядок шахматки"

    def test_no_selector_without_individual(self, client, doctor, rehab, staff, program):
        prescribe(doctor, program, "Эрго общая")
        client.force_login(rehab)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "Инструктор по желанию пациента" not in html
