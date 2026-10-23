"""Регрессия расписания для групп, заведённых на экране «Групповые занятия» (TZ.md, FR-CAT-1a,
FR-SCH-18, решения 81 и 82): раздел и группа создаются через экран, назначение приходит из
листа (tests/sheets.py), дальше — replan, шахматка, выбор времени, бассейн, перенос между
разделами, выключение и включение, карта. Плюс граничные случаи форм и права на POST-ы.

Сегодня — вторник 06.10.2026, следующий будний — среда 07.10. Курс из листа — с 05.10.
Инструкторы вымышленные: пара 2/2 Волков / Лебедева, Соколов — 5/2 (tests/conftest.py).
"""

from datetime import date, time
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Preset
from apps.catalog.models import (
    Equipment,
    GroupSession,
    Procedure,
    ProcedureKind,
    Section,
    SectionBehavior,
)
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.scheduling import board, board_days, services
from apps.scheduling.domain.model import IssueCode
from apps.scheduling.models import BoardPatient, Booking, BookingKind
from apps.staff.forms import board_sessions
from apps.staff.models import InstructorDuty
from tests.sheets import make_sheet

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
INDIVIDUAL_ROW = ("05.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", "")
BEHAVIORS = [SectionBehavior.LFK_GROUP, SectionBehavior.DS_GROUP, SectionBehavior.POOL]


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


# --- Помощники: всё через экран, как у специалиста ФР --------------------------------------


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def screen_section(client, name: str, behavior: str, order: int = 10) -> Section:
    response = client.post(
        reverse("scheduling:section_add"), {"name": name, "behavior": behavior, "order": order}
    )
    assert response.status_code == 302, response.content.decode()[:500]
    return Section.objects.get(name=name)


def group_data(section: Section, name: str, times: list[str], **fields) -> dict:
    return {
        "group-section": section.pk,
        "group-name": name,
        "group-card_label": fields.get("card_label", ""),
        "group-synonyms": fields.get("synonyms", []),
        "group-default_duration_min": fields.get("duration", "30"),
        "group-place": fields.get("place", ""),
        "group-department": fields.get("department", ""),
        "group-times": times,
    }


def screen_group(client, section: Section, name: str, times: list[str], **fields) -> Procedure:
    html = htmx(
        client,
        reverse("scheduling:group_add", args=[section.pk]),
        group_data(section, name, times, **fields),
    )
    assert f"Группа «{name}» заведена" in html, html[:2000]
    return Procedure.objects.get(name=name)


def edit_data(group: Procedure, **changes) -> dict:
    data = {
        "group-section": group.section_id,
        "group-name": group.name,
        "group-card_label": group.card_label,
        "group-synonyms": list(group.synonyms),
        "group-default_duration_min": str(group.default_duration_min or 30),
        "group-place": group.place,
        "group-department": group.department_id or "",
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value != ""}


def import_sheet(doctor, department, *rows, fio: str = "Тестова Анна Сергеевна", ib: str = "5402"):
    sheet = parse_sheet(make_sheet(fio=fio, ib=ib, rows=list(rows)))
    return import_prescription_sheet(doctor, department, sheet, attending_doctor=doctor)


def session(group: Procedure, start: time) -> GroupSession:
    return group.sessions.get(start_time=start)


def group_bookings(program, group: Procedure) -> set[tuple[date, time, str]]:
    return set(
        Booking.objects.filter(program=program, procedure=group).values_list(
            "date", "start", "kind"
        )
    )


def issue_codes(program) -> set[str]:
    program.refresh_from_db()
    return {issue["code"] for issue in program.schedule_issues}


def seat(day_board, instructor, slot):
    for column in day_board.columns:
        for cell in column.cells:
            if cell.slot.pk != slot.pk:
                continue
            for item in cell.seats:
                if item.instructor.pk == instructor.pk:
                    return item
    raise AssertionError("ячейки инструктора нет в шахматке")


@pytest.fixture
def screen(client, rehab, procedures, staff):
    """Специалист ФР на экране «Групповые занятия»; справочник и инструкторы — вымышленные."""
    board_days.ensure_boards()
    client.force_login(rehab)
    return client


# --- Весь путь для каждого поведения раздела ------------------------------------------------


class TestFullPathPerBehavior:
    @pytest.mark.parametrize("behavior", BEHAVIORS)
    def test_sheet_replan_board(self, screen, rehab, doctor, department, slots, staff, behavior):
        """Новый раздел → новая группа на 9:10 → лист назначений → группа в расписании,
        индивидуальное обходит 9:10, поставить его на 9:10 в шахматке нельзя."""
        section = screen_section(screen, "Новый раздел", behavior)
        group = screen_group(screen, section, "Тест-группа", ["09:10"], synonyms=["Аква-тест"])
        assert group.kind == behavior

        program = import_sheet(
            doctor, department, ("05.10.26", "Аква-тест 30 мин 1 р/д е/д", ""), INDIVIDUAL_ROW
        )

        item = program.prescriptions.get(procedure=group)
        assert item.procedure == group
        booked = group_bookings(program, group)
        assert (WED, time(9, 10), str(behavior)) in booked
        assert {start for _d, start, _k in booked} == {time(9, 10)}
        assert {d for d, _s, _k in booked} == set(program.course_dates()[1:-1])
        individual = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        assert individual.start != time(9, 10)
        busy = board.busy_json(board.occupied({program.pk}, WED)[program.pk])
        assert "9:10 Тест-группа" in busy
        with pytest.raises(board.BoardError):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], WED)
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

    def test_instructor_leads_new_lfk_group(self, screen, rehab, doctor, slots, staff, patient):
        """«Ведёт инструктор»: инструктор ведёт новую группу — ячейка занята подписью группы,
        пациента в неё не поставить; переименование группы видно в ячейке."""
        section = screen_section(screen, "Нейро", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, section, "Нейро-тест", ["09:10"])
        led = session(group, time(9, 10))
        assert led in board_sessions()
        assert led in board.group_sessions_for(slots["9:10"])

        board.add_duty(rehab, staff["sokolov"], slots["9:10"], WED, "GROUP_LEAD", group_session=led)

        cell = seat(board.board(rehab, WED), staff["sokolov"], slots["9:10"])
        assert cell.busy is not None and cell.busy.text == "Нейро-тест"
        other = patient(room="7")
        with pytest.raises(board.BoardError):
            board.place_patient(rehab, other, staff["sokolov"], slots["9:10"], WED)

        htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-name": "Нейро-тренинг Т"}),
        )
        cell = seat(board.board(rehab, WED), staff["sokolov"], slots["9:10"])
        assert cell.busy.text == "Нейро-тренинг Т"

    @pytest.mark.parametrize("behavior", [SectionBehavior.DS_GROUP, SectionBehavior.POOL])
    def test_instructor_cannot_lead_group_without_instructor(
        self, screen, rehab, slots, staff, behavior
    ):
        section = screen_section(screen, "Без ведущего", behavior)
        group = screen_group(screen, section, "Без ведущего-тест", ["09:10"])
        led = session(group, time(9, 10))

        assert led not in board_sessions()
        with pytest.raises(board.BoardError):
            board.add_duty(
                rehab, staff["sokolov"], slots["9:10"], WED, "GROUP_LEAD", group_session=led
            )
        assert not InstructorDuty.objects.exists()

    def test_lfk_wins_over_ds_with_same_synonym(self, screen, doctor, department):
        """Группы «Без инструктора» распознаются последними — и в новых разделах."""
        ds = screen_section(screen, "ДС-2", SectionBehavior.DS_GROUP)
        lfk = screen_section(screen, "ЛФК-2", SectionBehavior.LFK_GROUP)
        screen_group(screen, ds, "Колено ДС-тест", ["10:30"], synonyms=["колено"])
        wanted = screen_group(screen, lfk, "Колено ЛФК-тест", ["11:10"], synonyms=["колено"])

        program = import_sheet(doctor, department, ("05.10.26", "Колено 30 мин 1 р/д", ""))

        assert program.prescriptions.get().procedure == wanted


# --- Выбор времени группы (решение 81) ------------------------------------------------------


class TestGroupTimeChoice:
    @pytest.fixture
    def group(self, screen):
        section = screen_section(screen, "Спина-раздел", SectionBehavior.DS_GROUP)
        return screen_group(screen, section, "Спина-тест", ["11:10", "09:10"])

    def test_time_from_sheet(self, doctor, department, group):
        program = import_sheet(
            doctor, department, ("05.10.26", "Спина-тест в 11:10 30 мин 1 р/д", ""), INDIVIDUAL_ROW
        )

        item = program.prescriptions.get(procedure=group)
        assert item.group_session == session(group, time(11, 10))
        assert {s for _d, s, _k in group_bookings(program, group)} == {time(11, 10)}
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

    def test_unknown_time_in_sheet_warns(self, doctor, department, group):
        program = import_sheet(doctor, department, ("05.10.26", "Спина-тест в 12:00 30 мин", ""))

        item = program.prescriptions.get()
        assert item.group_session is None
        assert any(
            "нет занятия в 12:00" in w and "09:10, 11:10" in w for w in program.import_warnings
        )

    def test_auto_then_choice_on_program_page(self, client, doctor, department, group):
        program = import_sheet(doctor, department, ("05.10.26", "Спина-тест 30 мин 1 р/д", ""))
        item = program.prescriptions.get()
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)
        client.force_login(doctor)

        html = htmx(
            client,
            reverse("scheduling:program_group_time", args=[program.pk, item.pk]),
            {"session": session(group, time(11, 10)).pk},
        )

        assert 'id="schedule"' in html
        item.refresh_from_db()
        assert item.group_session == session(group, time(11, 10))
        assert {s for _d, s, _k in group_bookings(program, group)} == {time(11, 10)}
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

    def test_session_added_on_screen_offers_choice(self, screen, doctor, department):
        section = screen_section(screen, "Один раз", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, section, "Одна-тест", ["09:10"])
        program = import_sheet(doctor, department, ("05.10.26", "Одна-тест 30 мин 1 р/д", ""))
        assert IssueCode.GROUP_TIME_AUTO not in issue_codes(program)

        html = htmx(
            screen,
            reverse("scheduling:session_add"),
            {"procedure": group.pk, "start_time": "11:10", "duration_min": "30"},
        )

        assert "Расписание пересобрано у программ (1)" in html
        assert {s for _d, s, _k in group_bookings(program, group)} == {time(9, 10)}
        assert IssueCode.GROUP_TIME_AUTO in issue_codes(program)


# --- Бассейн без уточнения ------------------------------------------------------------------


@pytest.fixture
def generic_pool(db) -> Procedure:
    return Procedure.objects.create(
        name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
    )


class TestGenericPool:
    def test_group_from_new_pool_section_offered_and_chosen(
        self, screen, client, rehab, doctor, department, generic_pool
    ):
        water = screen_section(screen, "Вода", SectionBehavior.POOL)
        wanted = screen_group(screen, water, "Вода: шея-тест", ["15:00"])
        ds = screen_section(screen, "ДС-вода", SectionBehavior.DS_GROUP)
        not_pool = screen_group(screen, ds, "Вода ДС-тест", ["15:30"])
        program = import_sheet(doctor, department, ("05.10.26", "Бассейн 30 мин 1 р/д", ""))
        item = program.prescriptions.get()
        assert item.procedure == generic_pool
        assert IssueCode.POOL_TYPE_REQUIRED in issue_codes(program)
        assert wanted in services.pool_groups() and not_pool not in services.pool_groups()

        page = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "Вода: шея-тест" in page
        htmx(
            client,
            reverse("scheduling:program_pool_group", args=[program.pk, item.pk]),
            {"group": wanted.pk},
        )

        item.refresh_from_db()
        assert item.pool_group == wanted
        assert set(item.bookings.values_list("start", "kind")) == {(time(15, 0), "POOL")}

    def test_generic_pool_line_not_taken_by_new_pool_group(
        self, screen, doctor, department, generic_pool
    ):
        """Новая группа бассейна, чьё название по алфавиту раньше «Бассейн», не должна
        перехватывать строку «Бассейн» без уточнения."""
        water = screen_section(screen, "Вода", SectionBehavior.POOL)
        screen_group(screen, water, "Аква: шея-тест", ["15:00"])

        program = import_sheet(doctor, department, ("05.10.26", "Бассейн 30 мин 1 р/д", ""))

        assert program.prescriptions.get().procedure == generic_pool

    def test_new_pool_group_found_by_synonym_with_pool_word(
        self, screen, doctor, department, generic_pool
    ):
        water = screen_section(screen, "Вода", SectionBehavior.POOL)
        wanted = screen_group(
            screen, water, "ЛФК в воде: шея-тест", ["15:00"], synonyms=["Бассейн шея"]
        )

        program = import_sheet(doctor, department, ("05.10.26", "Бассейн шея 30 мин 1 р/д", ""))

        assert program.prescriptions.get().procedure == wanted


# --- Перенос между разделами одного поведения -----------------------------------------------


class TestMoveBetweenSections:
    @pytest.mark.parametrize("behavior", BEHAVIORS)
    def test_schedule_unchanged(self, screen, doctor, department, behavior):
        first = screen_section(screen, "Раздел А", behavior, order=10)
        second = screen_section(screen, "Раздел Б", behavior, order=11)
        group = screen_group(screen, first, "Переезд-тест", ["09:10", "11:10"])
        program = import_sheet(
            doctor, department, ("05.10.26", "Переезд-тест в 11:10 30 мин", ""), INDIVIDUAL_ROW
        )
        item = program.prescriptions.get(procedure=group)
        before = group_bookings(program, group)
        individual_before = set(
            program.bookings.filter(kind=BookingKind.INDIVIDUAL).values_list(
                "date", "start", "instructor"
            )
        )
        assert before

        html = htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-section": second.pk}),
        )

        assert "теперь в разделе «Раздел Б»" in html
        group.refresh_from_db()
        item.refresh_from_db()
        assert group.section == second and group.kind == behavior
        assert item.group_session == session(group, time(11, 10))
        assert group_bookings(program, group) == before
        assert (
            set(
                program.bookings.filter(kind=BookingKind.INDIVIDUAL).values_list(
                    "date", "start", "instructor"
                )
            )
            == individual_before
        )
        # Вкладки: группа ушла из первого раздела во второй, число занятий переехало с ней.
        page = screen.get(reverse("scheduling:group_schedule")).content.decode()
        assert f'?section={first.pk}">Раздел А <span class="badge text-bg-secondary">0' in page
        assert f'?section={second.pk}">Раздел Б <span class="badge text-bg-secondary">2' in page

    def test_led_group_duty_survives_move(self, screen, rehab, slots, staff):
        first = screen_section(screen, "Раздел А", SectionBehavior.LFK_GROUP)
        second = screen_section(screen, "Раздел Б", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, first, "Ведомая-тест", ["09:10"])
        led = session(group, time(9, 10))
        board.add_duty(rehab, staff["sokolov"], slots["9:10"], WED, "GROUP_LEAD", group_session=led)

        htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-section": second.pk}),
        )

        assert led in board_sessions()
        cell = seat(board.board(rehab, WED), staff["sokolov"], slots["9:10"])
        assert cell.busy is not None and cell.busy.text == "Ведомая-тест"


# --- Выключение группы с пациентами и включение обратно -------------------------------------


class TestDisableEnable:
    def test_disable_frees_time_then_enable_back(
        self, screen, rehab, doctor, department, slots, staff
    ):
        section = screen_section(screen, "Нейро", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, section, "Нейро-тест", ["09:10"])
        program = import_sheet(
            doctor, department, ("05.10.26", "Нейро-тест 30 мин 1 р/д", ""), INDIVIDUAL_ROW
        )
        assert group_bookings(program, group)

        # «Удалить» использованную группу — архив (решение 82): сначала шаг подтверждения.
        url = reverse("scheduling:group_delete", args=[group.pk])
        assert "Будет затронуто текущих программ: 1" in htmx(screen, url)
        html = htmx(screen, url, {"confirm": "1"})

        assert "в архиве" in html and "Расписание пересобрано у программ (1)" in html
        assert not group_bookings(program, group)
        assert IssueCode.NO_SESSIONS in issue_codes(program)
        assert session(group, time(9, 10)) not in board_sessions()
        # 9:10 у пациента теперь свободно — индивидуальное туда ставится.
        board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], WED)
        # Группа в архиве — свёрнутый блок внизу вкладки, её всё ещё можно открыть в панели.
        tab = screen.get(
            reverse("scheduling:group_schedule"), {"section": section.pk}
        ).content.decode()
        archive = tab[tab.index("В архиве (1)") :]
        assert "Нейро-тест" in archive
        assert screen.get(reverse("scheduling:group_edit", args=[group.pk])).status_code == 200

        htmx(screen, reverse("scheduling:group_restore", args=[group.pk]))
        group.refresh_from_db()
        assert group.is_active
        assert not group.sessions.filter(is_active=True).exists()  # времена включают по одному
        assert IssueCode.NO_SESSIONS in issue_codes(program)

        led = session(group, time(9, 10))
        html = htmx(screen, reverse("scheduling:session_restore", args=[led.pk]))
        assert "снова действует" in html
        html = htmx(
            screen,
            reverse("scheduling:session_edit", args=[led.pk]),
            {"edit-start_time": "11:10", "edit-duration_min": "30"},
        )
        assert "Расписание пересобрано у программ (1)" in html
        assert {s for _d, s, _k in group_bookings(program, group)} == {time(11, 10)}
        assert IssueCode.NO_SESSIONS not in issue_codes(program)

    def test_disable_keeps_other_patients(self, screen, rehab, doctor, department):
        """Выключение группы не трогает пациентов без неё."""
        section = screen_section(screen, "Нейро", SectionBehavior.DS_GROUP)
        group = screen_group(screen, section, "Нейро-тест", ["09:10"])
        other = screen_group(screen, section, "Другая-тест", ["10:30"])
        mine = import_sheet(doctor, department, ("05.10.26", "Нейро-тест 30 мин", ""))
        theirs = import_sheet(
            doctor,
            department,
            ("05.10.26", "Другая-тест 30 мин", ""),
            fio="Другова Вера Петровна",
            ib="7001",
        )
        before = group_bookings(theirs, other)

        result = services.save_group(rehab, _disabled(group))

        assert [p.pk for p in result.rescheduled.programs] == [mine.pk]
        assert group_bookings(theirs, other) == before

    def test_led_group_disabled_duty_ends_today(self, screen, rehab, slots, staff):
        """Решение 82: у выключенной группы «Ведение группы» заканчивается сегодня — постоянный
        распорядок по сегодня (прошлое в истории), будущий удаляется; слот свободен, экран
        называет инструктора."""
        section = screen_section(screen, "Нейро", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, section, "Нейро-тест", ["09:10"])
        led = session(group, time(9, 10))
        board.add_duty(rehab, staff["sokolov"], slots["9:10"], MON, "GROUP_LEAD", group_session=led)
        board.add_duty(rehab, staff["volkov"], slots["9:10"], WED, "GROUP_LEAD", group_session=led)

        result = services.save_group(rehab, _disabled(group))

        sokolov = InstructorDuty.objects.get(instructor=staff["sokolov"])
        assert (sokolov.valid_from, sokolov.valid_to) == (MON, TUE)
        assert sokolov.history.first().history_user == rehab
        assert not InstructorDuty.objects.filter(instructor=staff["volkov"]).exists()
        cell = seat(board.board(rehab, WED), staff["sokolov"], slots["9:10"])
        assert cell.busy is None
        assert sorted(str(item) for item in result.rescheduled.freed) == [
            "Волков 9:10 (Нейро-тест)",
            "Соколов 9:10 (Нейро-тест)",
        ]


def _disabled(group: Procedure) -> Procedure:
    group.refresh_from_db()
    group.is_active = False
    return group


# --- Карта (xlsx) ---------------------------------------------------------------------------


class TestCard:
    @pytest.mark.parametrize("behavior", BEHAVIORS)
    def test_card_prints_new_group_label(self, screen, client, doctor, department, behavior):
        section = screen_section(screen, "Для карты", behavior)
        screen_group(
            screen,
            section,
            "Карта-тест",
            ["09:10"],
            card_label="Группа Карта-тест",
            synonyms=["Картатест"],
        )
        program = import_sheet(
            doctor, department, ("05.10.26", "Картатест 30 мин 1 р/д е/д", ""), INDIVIDUAL_ROW
        )
        client.force_login(doctor)

        response = client.get(reverse("cards:download", args=[program.pk]))

        assert response.status_code == 200
        sheet = load_workbook(BytesIO(response.content)).active
        labels = [sheet[f"A{row}"].value for row in range(18, 34)]
        assert "Группа Карта-тест" in labels
        top = {
            sheet[f"B{row}"].value: sheet[f"C{row}"].value
            for row in range(4, 14)
            if sheet[f"B{row}"].value is not None
        }
        assert all(isinstance(value, time) for value in top)
        # Группы ЛФК и ДС печатаются подписью в карте; бассейн — названием группы.
        expected = "Карта-тест" if behavior == SectionBehavior.POOL else "Группа Карта-тест"
        assert expected in str(top.get(time(9, 10)))


# --- Граничные случаи форм ------------------------------------------------------------------


class TestGroupFormEdges:
    def url(self, section: Section) -> str:
        return reverse("scheduling:group_add", args=[section.pk])

    def test_empty_synonyms_ok(self, screen):
        ds = Section.objects.get(name="ДС")
        group = screen_group(screen, ds, "Без синонимов", ["09:00"], synonyms=["", "  "])
        assert group.synonyms == []

    @pytest.mark.parametrize("times", [[], [""], ["", "  "]])
    def test_no_times(self, screen, times):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "Без времени", times))
        assert "Укажите хотя бы одно время занятия." in html
        assert not Procedure.objects.filter(name="Без времени").exists()

    @pytest.mark.parametrize("value", ["25:00", "9:70", "утро", "9.10.11"])
    def test_bad_time(self, screen, value):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "Плохое время", [value]))
        assert f"Неверное время «{value}»" in html
        assert not Procedure.objects.filter(name="Плохое время").exists()

    @pytest.mark.parametrize("times", [["09:00", "09:00"], ["9:00", "09:00:00"]])
    def test_duplicate_times(self, screen, times):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "Дубль времени", times))
        assert "Время указано дважды" in html
        assert not Procedure.objects.filter(name="Дубль времени").exists()

    @pytest.mark.parametrize("name", ["ЭРГО ОБЩАЯ", " эрго общая ", "Эрго Общая"])
    def test_duplicate_name_other_case(self, screen, name):
        lfk = Section.objects.get(name="ЛФК")
        html = htmx(screen, self.url(lfk), group_data(lfk, name, ["09:00"]))
        assert "уже есть в справочнике среди общих" in html
        assert Procedure.objects.filter(name__iexact="эрго общая").count() == 1

    def test_duplicate_name_with_equipment_procedure(self, screen):
        """Название процедуры тренажёра тоже занято — ограничение одно на весь справочник."""
        lfk = Section.objects.get(name="ЛФК")
        html = htmx(screen, self.url(lfk), group_data(lfk, "ST-150", ["09:00"]))
        assert "уже есть в справочнике" in html

    def test_rename_to_taken_name_other_case(self, screen):
        lfk = Section.objects.get(name="ЛФК")
        group = screen_group(screen, lfk, "Переименуй-тест", ["09:00"])

        html = htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-name": "эрго ОБЩАЯ"}),
        )

        assert "уже есть в справочнике" in html
        group.refresh_from_db()
        assert group.name == "Переименуй-тест"

    def test_rename_own_case_allowed(self, screen):
        lfk = Section.objects.get(name="ЛФК")
        group = screen_group(screen, lfk, "регистр-тест", ["09:00"])

        html = htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-name": "Регистр-Тест"}),
        )

        assert "сохранена" in html
        group.refresh_from_db()
        assert group.name == "Регистр-Тест"

    @pytest.mark.parametrize("duration", ["-5", "abc", ""])
    def test_bad_duration(self, screen, duration):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "Длит-тест", ["09:00"], duration=duration))
        assert 'id="group-sessions"' in html
        assert not Procedure.objects.filter(name="Длит-тест").exists()

    def test_zero_duration_rejected(self, screen):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "Ноль-тест", ["09:00"], duration="0"))
        assert "Убедитесь, что это значение больше либо равно 1" in html
        assert not Procedure.objects.filter(name="Ноль-тест").exists()

    def test_empty_name(self, screen):
        ds = Section.objects.get(name="ДС")
        html = htmx(screen, self.url(ds), group_data(ds, "   ", ["09:00"]))
        assert "Обязательное поле" in html

    def test_long_name_with_empty_card_label(self, screen):
        """Подпись в карте короче названия (60 и 100): пустая подпись = название — ошибка
        должна быть понятной, а не 500."""
        ds = Section.objects.get(name="ДС")
        name = "Очень длинное название группы " * 3  # 90 символов
        html = htmx(screen, self.url(ds), group_data(ds, name.strip(), ["09:00"]))
        assert 'id="group-sessions"' in html
        assert not Procedure.objects.filter(name=name.strip()).exists()

    @pytest.mark.parametrize(
        ("start", "duration"), [("23:50", "30"), ("23:00", "120")], ids=["late", "long"]
    )
    def test_session_past_midnight(self, screen, start, duration):
        """Занятие, которое кончается после полуночи, — понятная ошибка, а не 500 при показе
        таблицы (GroupSession.end_time бросает ValueError)."""
        ds = Section.objects.get(name="ДС")
        response = screen.post(
            self.url(ds),
            group_data(ds, "Полночь-тест", [start], duration=duration),
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        assert not Procedure.objects.filter(name="Полночь-тест").exists()

    def test_department_of_other_department_rejected(self, screen, other_department):
        lfk = Section.objects.get(name="ЛФК")
        html = htmx(
            screen,
            self.url(lfk),
            group_data(lfk, "Чужое-тест", ["09:00"], department=other_department.pk),
        )
        assert "Выберите корректный вариант" in html
        assert not Procedure.objects.filter(name="Чужое-тест").exists()

    def test_move_common_group_to_other_department_rejected(self, screen, other_department):
        lfk = Section.objects.get(name="ЛФК")
        group = screen_group(screen, lfk, "Общая-тест", ["09:00"])

        htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-department": other_department.pk}),
        )

        group.refresh_from_db()
        assert group.department is None

    def test_section_in_post_wins_over_url(self, screen):
        """Раздел в форме — выбранный в списке; вид — из него."""
        lfk, pool = Section.objects.get(name="ЛФК"), Section.objects.get(name="Бассейн")
        htmx(screen, self.url(lfk), group_data(pool, "В бассейн-тест", ["15:00"]))
        group = Procedure.objects.get(name="В бассейн-тест")
        assert (group.section, group.kind) == (pool, ProcedureKind.POOL)

    def test_unknown_section_404(self, screen):
        assert screen.get(reverse("scheduling:group_add", args=[999999])).status_code == 404
        assert screen.post(reverse("scheduling:group_add", args=[999999]), {}).status_code == 404

    def test_department_choices(self, client, make_user, department, other_department):
        """Без отделения — только «Общая для центра»; администратор — любые отделения."""
        lfk = Section.objects.get(name="ЛФК")
        url = reverse("scheduling:group_add", args=[lfk.pk])
        client.force_login(make_user("lonely", rights=["group_schedule"]))
        html = client.get(url).content.decode()
        assert "Общая для центра" in html
        assert department.name not in html and other_department.name not in html

        client.force_login(make_user("boss", is_superuser=True))
        html = client.get(url).content.decode()
        assert department.name in html and other_department.name in html

    def test_counts_hide_other_department(self, screen, other_department):
        own = Section.objects.get(name="ЛФК")
        foreign = Procedure.objects.create(
            name="Чужая-тест", card_label="Чужая-тест", kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )  # fmt: skip
        GroupSession.objects.create(procedure=foreign, start_time=time(9, 0))
        page = screen.get(reverse("scheduling:group_schedule")).content.decode()
        assert (
            f'?section={own.pk}" aria-current="page">ЛФК <span class="badge text-bg-secondary">0'
            in page
        )


class TestSectionFormEdges:
    def test_empty_name(self, client, rehab):
        client.force_login(rehab)
        response = client.post(
            reverse("scheduling:section_add"), {"name": "  ", "behavior": "POOL", "order": "1"}
        )
        assert response.status_code == 200
        assert "Обязательное поле" in response.content.decode()

    @pytest.mark.parametrize("order", ["-1", "abc", "70000"])
    def test_bad_order(self, client, rehab, order):
        client.force_login(rehab)
        response = client.post(
            reverse("scheduling:section_add"),
            {"name": "Порядок", "behavior": "POOL", "order": order},
        )
        assert response.status_code == 200
        assert not Section.objects.filter(name="Порядок").exists()

    @pytest.mark.parametrize("behavior", ["", "INDIVIDUAL", "EQUIPMENT", "CARD_ONLY"])
    def test_bad_behavior(self, client, rehab, behavior):
        client.force_login(rehab)
        response = client.post(
            reverse("scheduling:section_add"),
            {"name": "Поведение", "behavior": behavior, "order": 1},
        )
        assert response.status_code == 200
        assert not Section.objects.filter(name="Поведение").exists()

    def test_nonempty_behavior_post_ignored_rename_saved(self, client, rehab):
        """Поле поведения у непустого раздела выключено — подделанный POST его не меняет,
        а переименование сохраняется."""
        ds = Section.objects.get(name="ДС")
        Procedure.objects.create(name="Г-тест", card_label="Г", kind=ProcedureKind.DS_GROUP)
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:section_edit", args=[ds.pk]),
            {"name": "ДС-переим", "behavior": "POOL", "order": "2"},
        )

        assert response.status_code == 302
        ds.refresh_from_db()
        assert (ds.name, ds.behavior) == ("ДС-переим", "DS_GROUP")
        assert Procedure.objects.get(name="Г-тест").kind == ProcedureKind.DS_GROUP

    def test_empty_behavior_change_via_screen(self, client, rehab):
        client.force_login(rehab)
        empty = Section.objects.create(name="Пустой", behavior="POOL", order=9)
        response = client.post(
            reverse("scheduling:section_edit", args=[empty.pk]),
            {"name": "Пустой", "behavior": "LFK_GROUP", "order": "9"},
        )
        assert response.status_code == 302
        empty.refresh_from_db()
        assert empty.behavior == "LFK_GROUP"

    def test_delete_needs_post_and_existing(self, client, rehab):
        client.force_login(rehab)
        empty = Section.objects.create(name="Пустой", behavior="POOL", order=9)
        assert client.get(reverse("scheduling:section_delete", args=[empty.pk])).status_code == 405
        assert client.post(reverse("scheduling:section_delete", args=[999999])).status_code == 404
        assert Section.objects.filter(pk=empty.pk).exists()

    @pytest.mark.parametrize("value", ["abc", "999999", "-1", "1" * 30, ""])
    def test_bad_section_param_shows_first(self, client, rehab, value):
        client.force_login(rehab)
        response = client.get(reverse("scheduling:group_schedule"), {"section": value})
        assert response.status_code == 200
        assert 'aria-current="page">ЛФК' in response.content.decode()

    def test_page_without_sections(self, client, rehab):
        Section.objects.all().delete()
        client.force_login(rehab)
        response = client.get(reverse("scheduling:group_schedule"))
        assert response.status_code == 200
        assert "Разделов пока нет" in response.content.decode()

    def test_delete_last_empty_section_then_group_from_loader_recreates(self, rehab):
        """Удалили пустой «Бассейн» — новая группа бассейна (команда, админка) заведёт раздел."""
        services.delete_section(rehab, Section.objects.get(name="Бассейн"))
        group = Procedure.objects.create(name="Б-тест", card_label="Б", kind=ProcedureKind.POOL)
        assert group.section.behavior == "POOL"


class TestEquipmentEdges:
    def test_name_taken_by_group(self, screen):
        html = htmx(
            screen,
            reverse("scheduling:equipment_add"),
            {
                "neweq-name": "эрго общая",
                "neweq-window_start": "13:00",
                "neweq-window_end": "15:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        )
        assert "уже есть в справочнике" in html
        assert not Equipment.objects.filter(name="эрго общая").exists()

    def test_empty_name(self, screen):
        html = htmx(
            screen,
            reverse("scheduling:equipment_add"),
            {
                "neweq-name": " ",
                "neweq-window_start": "13:00",
                "neweq-window_end": "15:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        )
        assert "Обязательное поле" in html

    def test_new_equipment_from_sheet(self, screen, doctor, department):
        """Тренажёр с экрана сразу назначается из листа и встаёт в своё окно."""
        htmx(
            screen,
            reverse("scheduling:equipment_add"),
            {
                "neweq-name": "Велотренажёр-тест",
                "neweq-card_label": "Вело-т",
                "neweq-synonyms": ["Велотест"],
                "neweq-window_start": "13:00",
                "neweq-window_end": "15:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        )

        program = import_sheet(doctor, department, ("05.10.26", "Велотест 15 мин 1 р/д", ""))

        item = program.prescriptions.get()
        assert item.procedure.equipment.name == "Велотренажёр-тест"
        starts = set(item.bookings.values_list("start", flat=True))
        assert starts and all(time(13) <= s < time(15) for s in starts)


# --- Права: POST-ы через клиент ---------------------------------------------------------------


@pytest.fixture
def targets(db, procedures):
    lfk = Section.objects.get(name="ЛФК")
    group = Procedure.objects.get(name="Эрго общая")
    led = GroupSession.objects.create(procedure=group, start_time=time(9, 10))
    empty = Section.objects.create(name="Пустой", behavior="POOL", order=9)
    return {"lfk": lfk, "group": group, "session": led, "empty": empty}


def post_requests(t) -> list[tuple[str, dict]]:
    return [
        (reverse("scheduling:section_add"), {"name": "Взлом", "behavior": "POOL", "order": 1}),
        (
            reverse("scheduling:section_edit", args=[t["lfk"].pk]),
            {"name": "Взлом", "behavior": "LFK_GROUP", "order": 1},
        ),
        (reverse("scheduling:section_delete", args=[t["empty"].pk]), {}),
        (
            reverse("scheduling:group_add", args=[t["lfk"].pk]),
            group_data(t["lfk"], "Взлом", ["09:00"]),
        ),
        (
            reverse("scheduling:group_edit", args=[t["group"].pk]),
            edit_data(t["group"], **{"group-name": "Взлом", "group-is_active": ""}),
        ),
        (
            reverse("scheduling:session_add"),
            {"procedure": t["group"].pk, "start_time": "12:00", "duration_min": "30"},
        ),
        (
            reverse("scheduling:session_edit", args=[t["session"].pk]),
            {"edit-start_time": "12:00", "edit-duration_min": "30"},
        ),
        (
            reverse("scheduling:equipment_add"),
            {
                "neweq-name": "Взлом",
                "neweq-window_start": "13:00",
                "neweq-window_end": "15:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        ),
        (
            reverse("scheduling:equipment_edit", args=[Equipment.objects.get().pk]),
            {
                "eq-window_start": "10:00",
                "eq-window_end": "12:00",
                "eq-step_min": "15",
                "eq-duration_min": "15",
                "eq-capacity": "9",
            },
        ),
    ]


def assert_nothing_changed(t) -> None:
    assert not Section.objects.filter(name="Взлом").exists()
    assert Section.objects.filter(pk=t["empty"].pk).exists()
    assert not Procedure.objects.filter(name="Взлом").exists()
    assert not Equipment.objects.filter(name="Взлом").exists()
    t["group"].refresh_from_db()
    assert t["group"].is_active and t["group"].name == "Эрго общая"
    assert list(GroupSession.objects.values_list("start_time", flat=True)) == [time(9, 10)]
    assert Equipment.objects.get().capacity == 1


class TestRights:
    @pytest.mark.parametrize("index", range(9))
    def test_doctor_403(self, client, doctor, targets, index):
        url, data = post_requests(targets)[index]
        client.force_login(doctor)
        assert client.post(url, data, HTTP_HX_REQUEST="true").status_code == 403
        assert_nothing_changed(targets)

    @pytest.mark.parametrize("index", range(9))
    def test_anonymous_redirected_to_login(self, client, targets, index):
        url, data = post_requests(targets)[index]
        response = client.post(url, data)
        assert response.status_code == 302 and "login" in response["Location"]
        assert_nothing_changed(targets)

    @pytest.mark.parametrize("name", ["section_add", "group_schedule", "equipment_add"], ids=str)
    def test_doctor_get_403(self, client, doctor, targets, name):
        client.force_login(doctor)
        assert client.get(reverse(f"scheduling:{name}")).status_code == 403

    def test_other_department_group_and_session_404(
        self, client, rehab, make_user, other_department
    ):
        """Группа другого отделения: правка группы и её занятия по прямой ссылке — 404,
        добавить ей время нельзя."""
        foreign = Procedure.objects.create(
            name="Чужая-тест", card_label="Чужая-тест", kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )  # fmt: skip
        led = GroupSession.objects.create(procedure=foreign, start_time=time(9, 0))
        client.force_login(rehab)

        edit = client.post(
            reverse("scheduling:group_edit", args=[foreign.pk]),
            edit_data(foreign, **{"group-is_active": ""}),
        )
        sess = client.post(
            reverse("scheduling:session_edit", args=[led.pk]),
            {"edit-start_time": "12:00", "edit-duration_min": "30"},
        )
        add = client.post(
            reverse("scheduling:session_add"),
            {"procedure": foreign.pk, "start_time": "12:00", "duration_min": "30"},
            HTTP_HX_REQUEST="true",
        )
        delete = client.post(
            reverse("scheduling:group_delete", args=[foreign.pk]), {"confirm": "1"}
        )

        assert edit.status_code == 404 and sess.status_code == 404
        assert add.status_code == 404 and delete.status_code == 404
        foreign.refresh_from_db()
        assert foreign.is_active
        assert list(foreign.sessions.values_list("start_time", flat=True)) == [time(9, 0)]

    def test_admin_can_do_everything(self, client, admin_user, other_department, targets):
        client.force_login(admin_user)
        lfk = targets["lfk"]
        html = htmx(
            client,
            reverse("scheduling:group_add", args=[lfk.pk]),
            group_data(lfk, "Админская-тест", ["09:00"], department=other_department.pk),
        )
        assert "заведена" in html
        assert Procedure.objects.get(name="Админская-тест").department == other_department
        response = client.post(reverse("scheduling:section_delete", args=[targets["empty"].pk]))
        assert response.status_code == 302
        assert not Section.objects.filter(pk=targets["empty"].pk).exists()

    def test_rehab_other_department_member_can_edit_common_group(
        self, client, make_user, other_department, targets
    ):
        """Общая группа центра — правит любой со правом «Групповые занятия»."""
        stranger = make_user("stranger", (other_department, Preset.REHAB))
        client.force_login(stranger)
        group = targets["group"]
        html = htmx(
            client,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-place": "зал 3"}),
        )
        assert "сохранена" in html
        group.refresh_from_db()
        assert group.place == "зал 3"


# --- Целостность ----------------------------------------------------------------------------


class TestIntegrity:
    def test_history_on_screen_actions(self, screen, rehab):
        section = screen_section(screen, "История", SectionBehavior.DS_GROUP)
        group = screen_group(screen, section, "История-тест", ["09:00"])
        led = session(group, time(9, 0))
        htmx(
            screen,
            reverse("scheduling:group_edit", args=[group.pk]),
            edit_data(group, **{"group-name": "История-2"}),
        )
        # Ни разу не использованная группа удаляется совсем — с записью в журнал.
        htmx(screen, reverse("scheduling:group_delete", args=[group.pk]), {"confirm": "1"})

        assert section.history.first().history_user == rehab
        assert not Procedure.objects.filter(pk=group.pk).exists()
        assert group.history.count() == 3
        assert all(h.history_user == rehab for h in group.history.all())
        assert led.history.count() == 2 and all(h.history_user == rehab for h in led.history.all())

    def test_unique_session_start_in_db(self, rehab):
        group = services.create_group(
            rehab,
            Procedure(section=Section.objects.get(name="ДС"), name="Уник-тест"),
            [time(9, 0)],
        ).group
        with pytest.raises(IntegrityError), transaction.atomic():
            GroupSession.objects.create(procedure=group, start_time=time(9, 0))

    def test_create_group_atomic_on_bad_time(self, rehab, monkeypatch):
        """Ошибка на втором занятии не оставляет группу без занятий."""
        original = GroupSession.full_clean
        calls = {"n": 0}

        def failing(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValidationError("сбой")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(GroupSession, "full_clean", failing)

        with pytest.raises(ValidationError):
            services.create_group(
                rehab,
                Procedure(section=Section.objects.get(name="ДС"), name="Атом-тест"),
                [time(9, 0), time(10, 0)],
            )
        assert not Procedure.objects.filter(name="Атом-тест").exists()

    def test_board_patient_unplaced_when_group_added_over_individual(
        self, screen, rehab, doctor, department, slots, staff
    ):
        """Новое занятие группы ложится на индивидуальное в шахматке — пациент уходит
        в «Не распределены» и это видно на экране."""
        section = screen_section(screen, "Нейро", SectionBehavior.LFK_GROUP)
        group = screen_group(screen, section, "Нейро-тест", ["09:10"])
        program = import_sheet(
            doctor, department, ("05.10.26", "Нейро-тест 30 мин 1 р/д", ""), INDIVIDUAL_ROW
        )
        individual = Booking.objects.get(program=program, date=WED, kind=BookingKind.INDIVIDUAL)
        led = session(group, time(9, 10))

        html = htmx(
            screen,
            reverse("scheduling:session_edit", args=[led.pk]),
            {
                "edit-start_time": f"{individual.start:%H:%M}",
                "edit-duration_min": "30",
            },
        )

        assert "Не распределены" in html or "не распределены" in html
        assert BoardPatient.objects.get(program=program, date=WED).unplaced == 1


class TestMoreEdges:
    def test_too_long_synonym(self, screen):
        ds = Section.objects.get(name="ДС")
        html = htmx(
            screen,
            reverse("scheduling:group_add", args=[ds.pk]),
            group_data(ds, "Синоним-тест", ["09:00"], synonyms=["я" * 101]),
        )
        assert "длиннее 100 символов" in html
        assert not Procedure.objects.filter(name="Синоним-тест").exists()

    def test_generic_pool_not_editable_as_group(self, screen, generic_pool):
        url = reverse("scheduling:group_edit", args=[generic_pool.pk])
        assert screen.get(url).status_code == 404

    def test_other_department_group_not_recognized_in_sheet(
        self, client, admin_user, doctor, department, other_department
    ):
        """Группа чужого отделения с тем же синонимом не распознаётся в листе своего."""
        client.force_login(admin_user)
        ds = Section.objects.get(name="ДС")
        screen_group(
            client, ds, "Чужая-тест", ["09:00"], synonyms=["Особая"],
            department=other_department.pk,
        )  # fmt: skip

        program = import_sheet(doctor, department, ("05.10.26", "Особая 30 мин 1 р/д", ""))

        assert program.prescriptions.get().procedure is None

    def test_department_group_recognized_in_own_sheet(self, screen, doctor, department):
        ds = Section.objects.get(name="ДС")
        group = screen_group(
            screen, ds, "Своя-тест", ["09:00"], synonyms=["Особая"], department=department.pk
        )

        program = import_sheet(doctor, department, ("05.10.26", "Особая 30 мин 1 р/д", ""))

        assert program.prescriptions.get().procedure == group
        assert group_bookings(program, group)
