"""Разделы групповых занятий (TZ.md, FR-CAT-1a, FR-SCH-18, решение 82): разделы, новые группы
и тренажёры на экране «Групповые занятия» и главное — расписание с такими группами работает
как раньше: поведение раздела = прежний вид группы.

Сегодня в тестах — вторник 06.10.2026, следующий будний — среда 07.10. Инструкторы и
листы назначений — вымышленные (tests/conftest.py, tests/sheets.py).
"""

from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import Preset
from apps.catalog.models import (
    Equipment,
    GroupSession,
    Procedure,
    ProcedureKind,
    Section,
    SectionBehavior,
)
from apps.catalog.services import search_procedures
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.programs.models import Prescription
from apps.programs.services import add_prescription
from apps.scheduling import board, board_days, services
from apps.scheduling.domain.model import IssueCode
from apps.scheduling.models import BoardPatient, Booking, BookingKind
from apps.staff.forms import board_sessions
from apps.staff.models import InstructorBlock, InstructorDuty
from tests.sheets import make_sheet

TUE, WED = date(2026, 10, 6), date(2026, 10, 7)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


def new_section(user, name: str, behavior: str, order: int = 10) -> Section:
    return services.save_section(user, Section(name=name, behavior=behavior, order=order))


def new_group(user, section: Section, name: str, *times: time, **fields) -> Procedure:
    group = Procedure(section=section, name=name, default_duration_min=30, **fields)
    return services.create_group(user, group, list(times)).group


def session(group: Procedure, start: time) -> GroupSession:
    return group.sessions.get(start_time=start)


def issue_codes(program) -> set[str]:
    program.refresh_from_db()
    return {issue["code"] for issue in program.schedule_issues}


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


# --- Данные: миграция, модель, начальная загрузка -----------------------------------------


class TestModel:
    def test_starting_sections_from_migration(self, db):
        """Миграция catalog.0008 завела разделы «ЛФК», «ДС», «Бассейн» в этом порядке."""
        assert [(s.name, s.behavior) for s in Section.objects.all()] == [
            ("ЛФК", "LFK_GROUP"),
            ("ДС", "DS_GROUP"),
            ("Бассейн", "POOL"),
        ]

    def test_group_without_section_goes_to_first_section_of_its_kind(self, db):
        group = Procedure.objects.create(name="Г", card_label="Г", kind=ProcedureKind.DS_GROUP)
        pool = Procedure.objects.create(
            name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
        )
        individual = Procedure.objects.create(
            name="Инд", card_label="Инд", kind=ProcedureKind.INDIVIDUAL
        )

        assert group.section.name == "ДС"
        assert pool.section is None and individual.section is None

    def test_default_section_created_when_missing(self, db):
        Section.objects.filter(behavior=SectionBehavior.POOL).delete()
        Section.objects.create(name="Бассейн", behavior=SectionBehavior.LFK_GROUP, order=7)

        section = Section.default_for(ProcedureKind.POOL)

        assert (section.name, section.behavior, section.order) == ("Бассейн 2", "POOL", 8)

    def test_kind_must_match_section_behavior(self, db):
        lfk = Section.objects.get(name="ЛФК")
        group = Procedure(name="Г", card_label="Г", kind=ProcedureKind.POOL, section=lfk)
        with pytest.raises(ValidationError, match="поведением"):
            group.full_clean()
        group.kind = ProcedureKind.INDIVIDUAL
        with pytest.raises(ValidationError, match="только у групп"):
            group.full_clean()

    def test_section_behavior_fixed_when_not_empty(self, db):
        ds = Section.objects.get(name="ДС")
        Procedure.objects.create(name="Г", card_label="Г", kind=ProcedureKind.DS_GROUP)
        ds.behavior = SectionBehavior.POOL
        with pytest.raises(ValidationError, match="поведение раздела не меняется"):
            ds.full_clean()


# --- Разделы ------------------------------------------------------------------------------


class TestSections:
    def test_create_and_rename(self, rehab):
        section = new_section(rehab, "Эрго", SectionBehavior.LFK_GROUP, order=4)
        section.name = "Эрготерапия"
        services.save_section(rehab, section)

        section.refresh_from_db()
        assert section.name == "Эрготерапия"
        assert section.history.first().history_user == rehab

    def test_behavior_change_only_when_empty(self, rehab):
        section = new_section(rehab, "Новые", SectionBehavior.LFK_GROUP)
        section.behavior = SectionBehavior.DS_GROUP
        services.save_section(rehab, section)  # пустой — можно

        new_group(rehab, section, "Группа Н", time(9, 0))
        section.behavior = SectionBehavior.POOL
        with pytest.raises(services.ScheduleError, match="есть группы"):
            services.save_section(rehab, section)
        section.refresh_from_db()
        assert section.behavior == SectionBehavior.DS_GROUP

    def test_delete_only_empty(self, rehab):
        section = new_section(rehab, "Новые", SectionBehavior.DS_GROUP)
        group = new_group(rehab, section, "Группа Н", time(9, 0))
        group.is_active = False
        services.save_group(rehab, group)

        with pytest.raises(services.ScheduleError, match="только пустой"):
            services.delete_section(rehab, section)  # выключенная группа — тоже группа

        empty = new_section(rehab, "Пустой", SectionBehavior.POOL)
        services.delete_section(rehab, empty)
        assert not Section.objects.filter(pk=empty.pk).exists()

    def test_duplicate_name(self, rehab):
        with pytest.raises(ValidationError):
            new_section(rehab, "ЛФК", SectionBehavior.LFK_GROUP)

    def test_doctor_has_no_right(self, doctor):
        with pytest.raises(PermissionDenied):
            new_section(doctor, "Новые", SectionBehavior.LFK_GROUP)
        with pytest.raises(PermissionDenied):
            services.delete_section(doctor, Section.objects.get(name="ДС"))


# --- Группы -------------------------------------------------------------------------------


class TestCreateGroup:
    def test_kind_and_sessions_from_section(self, rehab):
        section = new_section(rehab, "Без инструктора", SectionBehavior.DS_GROUP)

        group = new_group(
            rehab, section, " Спина-тест ", time(16, 30), time(8, 15), synonyms=["Спина Т"]
        )

        assert group.kind == ProcedureKind.DS_GROUP and group.section == section
        assert group.name == "Спина-тест" and group.card_label == "Спина-тест"
        assert [(s.start_time, s.duration_min) for s in group.sessions.order_by("start_time")] == [
            (time(8, 15), 30),
            (time(16, 30), 30),
        ]
        assert group.history.first().history_user == rehab
        assert session(group, time(8, 15)).history.first().history_user == rehab

    def test_times_required_and_unique(self, rehab):
        section = Section.objects.get(name="ДС")
        with pytest.raises(services.ScheduleError, match="хотя бы одно"):
            new_group(rehab, section, "Г")
        with pytest.raises(services.ScheduleError, match="дважды: 9:00"):
            new_group(rehab, section, "Г", time(9, 0), time(9, 0))
        assert not Procedure.objects.filter(name="Г").exists()

    def test_name_unique_in_department(self, rehab, department, procedures):
        section = Section.objects.get(name="ЛФК")
        with pytest.raises(services.ScheduleError, match="уже есть в справочнике среди общих"):
            new_group(rehab, section, "эрго общая", time(9, 0))
        # В отделении — своё пространство названий, как у ограничения модели.
        group = new_group(rehab, section, "Эрго общая", time(9, 0), department=department)
        assert group.department == department

    def test_department_only_own(self, rehab, other_department, admin_user):
        section = Section.objects.get(name="ЛФК")
        with pytest.raises(PermissionDenied):
            new_group(rehab, section, "Чужая", time(9, 0), department=other_department)
        group = new_group(admin_user, section, "Чужая", time(9, 0), department=other_department)
        assert group.department == other_department

    def test_doctor_has_no_right(self, doctor):
        with pytest.raises(PermissionDenied):
            new_group(doctor, Section.objects.get(name="ЛФК"), "Г", time(9, 0))


class TestEditGroup:
    def test_move_only_to_same_behavior(self, rehab):
        lfk = Section.objects.get(name="ЛФК")
        other_lfk = new_section(rehab, "Эрго", SectionBehavior.LFK_GROUP)
        group = new_group(rehab, lfk, "Группа Н", time(9, 10))

        group.section = Section.objects.get(name="ДС")
        with pytest.raises(services.ScheduleError, match="«Ведёт инструктор»"):
            services.save_group(rehab, group)
        group.refresh_from_db()
        assert group.section == lfk and group.kind == ProcedureKind.LFK_GROUP

        group.section = other_lfk
        services.save_group(rehab, group)
        group.refresh_from_db()
        assert group.section == other_lfk and group.kind == ProcedureKind.LFK_GROUP

    def test_other_department_group_is_protected(self, make_user, other_department):
        stranger = make_user("stranger", (other_department, Preset.REHAB))
        group = new_group(
            stranger, Section.objects.get(name="ЛФК"), "Г", time(9, 0), department=other_department
        )
        rehab_here = make_user("here", is_superuser=False, rights=["group_schedule"])

        group.name = "Переименована"
        with pytest.raises(PermissionDenied):
            services.save_group(rehab_here, group)

    def test_disable_group(self, rehab, doctor, make_program):
        """Выключение: все занятия выключены, выбор времени сброшен, у программы — конфликт
        «у группы нет занятий»; группы нет в поиске и выборе (решение 82)."""
        section = Section.objects.get(name="ДС")
        group = new_group(rehab, section, "Спина-тест", time(8, 15), time(11, 10))
        program = make_program(full_name="Выключенов Тест Тестович")
        item = add_prescription(doctor, Prescription(program=program, procedure=group))
        services.choose_group_time(doctor, item, session(group, time(11, 10)))
        assert program.bookings.filter(kind=BookingKind.DS_GROUP).exists()

        group.is_active = False
        result = services.save_group(rehab, group)

        assert not group.sessions.filter(is_active=True).exists()
        assert [p.pk for p in result.rescheduled.programs] == [program.pk]
        assert [p.pk for p in result.rescheduled.reset] == [item.pk]
        item.refresh_from_db()
        assert item.group_session is None
        assert item.history.first().history_user == rehab
        assert not program.bookings.filter(kind=BookingKind.DS_GROUP).exists()
        assert IssueCode.NO_SESSIONS in issue_codes(program)
        assert search_procedures(program.department, "Спина-тест") == []
        assert group not in services.group_schedule_procedures(rehab)

    def test_enable_back_keeps_sessions_off(self, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0))
        group.is_active = False
        services.save_group(rehab, group)

        group.is_active = True
        result = services.save_group(rehab, group)

        assert result.rescheduled is None
        assert group in services.group_schedule_procedures(rehab)
        assert not group.sessions.filter(is_active=True).exists()


# --- Регрессия: поведение раздела = прежний вид группы ------------------------------------


class TestBehavior:
    def test_led_by_instructor(self, rehab, doctor, patient, slots, staff):
        """Группа в новом разделе «Ведёт инструктор»: инструктор может её вести, её занятие
        не даёт поставить индивидуальное пациента на это время."""
        board_days.ensure_boards()
        section = new_section(rehab, "Нейро", SectionBehavior.LFK_GROUP)
        group = new_group(rehab, section, "Нейро-тест", time(9, 10))
        led = session(group, time(9, 10))
        assert led in board_sessions()
        assert led in board.group_sessions_for(slots["9:10"])

        program = patient(start_date=TUE)
        assert Booking.objects.filter(program=program, date=WED, start=time(9, 10)).exists()
        add_prescription(doctor, Prescription(program=program, procedure=group))

        assert program.bookings.filter(date=WED, kind=BookingKind.LFK_GROUP).exists()
        entry = BoardPatient.objects.get(program=program, date=WED)
        assert entry.unplaced == 1  # индивидуальное ушло с 9:10 — там группа
        with pytest.raises(board.BoardError):
            board.place_patient(rehab, program, staff["sokolov"], slots["9:10"], WED)
        board.place_patient(rehab, program, staff["sokolov"], slots["10:30"], WED)

    @pytest.mark.parametrize("behavior", [SectionBehavior.DS_GROUP, SectionBehavior.POOL])
    def test_without_instructor(self, rehab, slots, behavior):
        section = new_section(rehab, "Новый раздел", behavior)
        group = new_group(rehab, section, "Группа-тест", time(9, 10))
        led = session(group, time(9, 10))

        assert led not in board_sessions()
        assert led not in board.group_sessions_for(slots["9:10"])
        assert (group in services.pool_groups()) == (behavior == SectionBehavior.POOL)

    def test_pool_group_chosen_for_generic_pool(self, rehab, doctor, make_program):
        generic = Procedure.objects.create(
            name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
        )
        section = new_section(rehab, "Вода", SectionBehavior.POOL)
        group = new_group(rehab, section, "ЛФК в воде: шея", time(15, 0))
        program = make_program()
        item = add_prescription(doctor, Prescription(program=program, procedure=generic))

        services.choose_pool_group(rehab, item, group)

        assert set(item.bookings.values_list("start", flat=True)) == {time(15, 0)}
        assert set(item.bookings.values_list("kind", flat=True)) == {BookingKind.POOL}


class TestSheetRecognition:
    def test_new_group_found_by_synonym(self, rehab, doctor, department):
        section = new_section(rehab, "Ортопедия", SectionBehavior.DS_GROUP)
        group = new_group(
            rehab, section, "Ортопедия: стопа", time(10, 0), synonyms=["Стопа ортопед"]
        )
        sheet = parse_sheet(make_sheet(rows=[("05.10.26", "Стопа ортопед 30 мин 1 р/д", "")]))

        program = import_prescription_sheet(doctor, department, sheet, attending_doctor=doctor)

        assert program.prescriptions.get().procedure == group

    def test_disabled_group_not_recognized(self, rehab, doctor, department):
        group = new_group(
            rehab, Section.objects.get(name="ЛФК"), "Стопа-тест", time(10, 0),
            synonyms=["Стопа ортопед"],
        )  # fmt: skip
        group.is_active = False
        services.save_group(rehab, group)
        sheet = parse_sheet(make_sheet(rows=[("05.10.26", "Стопа ортопед 30 мин 1 р/д", "")]))

        program = import_prescription_sheet(doctor, department, sheet, attending_doctor=doctor)

        assert program.prescriptions.get().procedure is None


# --- Тренажёры ----------------------------------------------------------------------------


class TestEquipment:
    def test_create_with_procedure(self, rehab, department):
        equipment = services.create_equipment(
            rehab,
            Equipment(name="Тренажёр Т", capacity=2),
            card_label="Тр. Т",
            synonyms=["тренажер т"],
        )

        procedure = equipment.procedures.get()
        assert procedure.kind == ProcedureKind.EQUIPMENT and procedure.card_label == "Тр. Т"
        assert procedure.place == "зона БОС терапии" and procedure.default_duration_min == 15
        assert procedure.section is None
        assert search_procedures(department, "тренажер т") == [procedure]
        assert equipment.history.first().history_user == rehab

    def test_duplicate_name(self, rehab, procedures):
        with pytest.raises(services.ScheduleError, match="уже есть"):
            services.create_equipment(rehab, Equipment(name="ST-150"))

    def test_doctor_has_no_right(self, doctor):
        with pytest.raises(PermissionDenied):
            services.create_equipment(doctor, Equipment(name="Т"))


# --- Экран --------------------------------------------------------------------------------


class TestScreen:
    def test_doctor_forbidden(self, client, doctor):
        client.force_login(doctor)
        ds = Section.objects.get(name="ДС")
        assert client.get(reverse("scheduling:group_schedule")).status_code == 403
        assert client.get(reverse("scheduling:group_add", args=[ds.pk])).status_code == 403
        assert client.post(reverse("scheduling:section_add"), {}).status_code == 403
        assert client.post(reverse("scheduling:equipment_add"), {}).status_code == 403

    def test_section_add_redirects_to_new_tab(self, client, rehab):
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:section_add"),
            {"name": "Эрго", "behavior": "LFK_GROUP", "order": "4"},
            follow=True,
        )

        section = Section.objects.get(name="Эрго")
        html = response.content.decode()
        assert f'?section={section.pk}" aria-current="page">Эрго' in html
        assert "Раздел «Эрго» сохранён" in html

    def test_section_errors_on_page(self, client, rehab):
        ds = Section.objects.get(name="ДС")
        Procedure.objects.create(name="Г", card_label="Г", kind=ProcedureKind.DS_GROUP)
        client.force_login(rehab)

        html = client.get(reverse("scheduling:section_edit", args=[ds.pk])).content.decode()
        assert "поведение не меняется" in html and html.count("disabled>") == 3

        response = client.post(
            reverse("scheduling:section_add"), {"name": "ДС", "behavior": "POOL", "order": "1"}
        )
        assert response.status_code == 200
        assert "Раздел с таким названием уже есть" in response.content.decode()

    def test_section_delete(self, client, rehab):
        ds = Section.objects.get(name="ДС")
        Procedure.objects.create(name="Г", card_label="Г", kind=ProcedureKind.DS_GROUP)
        empty = Section.objects.create(name="Пустой", behavior="POOL", order=9)
        client.force_login(rehab)

        html = client.post(
            reverse("scheduling:section_delete", args=[ds.pk]), follow=True
        ).content.decode()
        assert "только пустой раздел" in html
        client.post(reverse("scheduling:section_delete", args=[empty.pk]))

        assert Section.objects.filter(pk=ds.pk).exists()
        assert not Section.objects.filter(pk=empty.pk).exists()

    def test_group_add(self, client, rehab, department):
        ds = Section.objects.get(name="ДС")
        client.force_login(rehab)
        url = reverse("scheduling:group_add", args=[ds.pk])
        form = client.get(url, HTTP_HX_REQUEST="true").content.decode()
        assert 'id="group-sessions"' in form and "Общая для центра" in form

        html = htmx(
            client,
            url,
            {
                "group-section": ds.pk,
                "group-name": "Плечо-тест",
                "group-card_label": "",
                "group-synonyms": ["Плечо Т", "", "Плечо Т", "  плечо т2 "],
                "group-default_duration_min": "40",
                "group-place": "зал 2",
                "group-department": department.pk,
                "group-times": ["16:00", "", "08:15"],
            },
        )

        group = Procedure.objects.get(name="Плечо-тест")
        assert "Группа «Плечо-тест» заведена" in html
        assert group.synonyms == ["Плечо Т", "плечо т2"]
        assert (group.kind, group.department, group.card_label) == (
            "DS_GROUP",
            department,
            "Плечо-тест",
        )
        assert [(s.start_time, s.duration_min) for s in group.sessions.order_by("start_time")] == [
            (time(8, 15), 40),
            (time(16, 0), 40),
        ]
        assert html.index("8:15–8:55") < html.index("16:00–16:40")

    def test_group_add_errors_in_fragment(self, client, rehab, procedures):
        lfk = Section.objects.get(name="ЛФК")
        client.force_login(rehab)
        data = {
            "group-section": lfk.pk,
            "group-name": "Эрго общая",
            "group-default_duration_min": "30",
            "group-times": ["09:00", "9:00"],
        }

        html = htmx(client, reverse("scheduling:group_add", args=[lfk.pk]), data)

        assert 'id="group-sessions"' in html
        assert "уже есть в справочнике" in html and "Время указано дважды: 09:00" in html
        assert Procedure.objects.filter(name="Эрго общая").count() == 1

    def test_group_edit_move_error_in_fragment(self, client, rehab):
        lfk = Section.objects.get(name="ЛФК")
        group = new_group(rehab, lfk, "Г", time(9, 0), synonyms=["Г1"])
        client.force_login(rehab)
        url = reverse("scheduling:group_edit", args=[group.pk])
        form = client.get(url).content.decode()
        assert 'value="Г1"' in form and "group-times" not in form

        html = htmx(
            client,
            url,
            {
                "group-section": Section.objects.get(name="Бассейн").pk,
                "group-name": "Г",
                "group-default_duration_min": "30",
                "group-is_active": "on",
            },
        )

        assert "выберите раздел с тем же поведением" in html
        group.refresh_from_db()
        assert group.section == lfk

    def test_group_archive_shows_rescheduled(self, client, rehab, doctor, make_program):
        ds = Section.objects.get(name="ДС")
        group = new_group(rehab, ds, "Спина-тест", time(8, 15))
        program = make_program(full_name="Экранов Тест Тестович")
        add_prescription(doctor, Prescription(program=program, procedure=group))
        client.force_login(rehab)
        url = reverse("scheduling:group_delete", args=[group.pk])

        step = htmx(client, url)
        assert "уйдёт в архив" in step and "Будет затронуто текущих программ: 1" in step
        group.refresh_from_db()
        assert group.is_active  # без подтверждения ничего не меняется

        html = htmx(client, url, {"confirm": "1"})

        assert "в архиве" in html and "Расписание пересобрано у программ (1)" in html
        assert "Экранов" in html and "Вернуть из архива" in html
        table = html[html.index('id="group-sessions"') :]
        assert 'hx-swap-oob="true"' in table[:200] and "В архиве (1)" in table
        group.refresh_from_db()
        assert not group.is_active and not group.sessions.filter(is_active=True).exists()

    def test_other_department_group_not_visible(self, client, rehab, other_department):
        group = Procedure.objects.create(
            name="Чужая", card_label="Чужая", kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )  # fmt: skip
        client.force_login(rehab)

        assert client.get(reverse("scheduling:group_edit", args=[group.pk])).status_code == 404
        assert "Чужая" not in client.get(reverse("scheduling:group_schedule")).content.decode()

    def test_session_add_in_panel(self, client, rehab):
        ds = Section.objects.get(name="ДС")
        spine = new_group(rehab, ds, "Спина-тест", time(9, 0))
        client.force_login(rehab)
        url = reverse("scheduling:session_add")
        data = {"procedure": spine.pk, "start_time": "12:00", "duration_min": "30"}

        html = htmx(client, url, data)

        assert html.lstrip().startswith('<div id="group-panel"') and "Сохранено" in html
        assert 'id="group-sessions" data-panel-table hx-swap-oob="true"' in html
        assert session(spine, time(12, 0))
        html = htmx(client, url, data)  # то же время — ошибка в панели
        assert html.lstrip().startswith('<div id="group-panel"') and "уже существует" in html

    def test_equipment_add(self, client, rehab):
        client.force_login(rehab)
        url = reverse("scheduling:equipment_add")
        assert "Новый тренажёр" in client.get(url).content.decode()

        html = htmx(
            client,
            url,
            {
                "neweq-name": "Велотренажёр",
                "neweq-card_label": "",
                "neweq-synonyms": ["Вело", ""],
                "neweq-window_start": "13:00",
                "neweq-window_end": "15:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        )

        assert "Тренажёр «Велотренажёр» заведён" in html
        procedure = Procedure.objects.get(name="Велотренажёр")
        assert procedure.equipment.name == "Велотренажёр" and procedure.synonyms == ["Вело"]

    def test_equipment_add_errors_in_fragment(self, client, rehab):
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("scheduling:equipment_add"),
            {
                "neweq-name": "Т",
                "neweq-window_start": "15:00",
                "neweq-window_end": "13:00",
                "neweq-step_min": "15",
                "neweq-duration_min": "15",
                "neweq-capacity": "1",
            },
        )
        assert 'id="equipment-table"' in html and "Окно должно заканчиваться позже" in html
        assert not Equipment.objects.filter(name="Т").exists()


# --- Исправления по ревью: ведение группы, полночь, регистр, админка ------------------------


class TestGroupLeadEnds:
    """Выключили или перенесли занятие — «Ведение группы» на него заканчивается сегодня
    (решение 82)."""

    def lead(self, rehab, staff, slots, group, start=time(9, 10), day=date(2026, 10, 5)):
        board.add_duty(
            rehab, staff["sokolov"], slots["9:10"], day, "GROUP_LEAD",
            group_session=session(group, start),
        )  # fmt: skip
        return InstructorDuty.objects.get(instructor=staff["sokolov"])

    def test_session_disabled(self, rehab, staff, slots):
        group = new_group(rehab, Section.objects.get(name="ЛФК"), "Г", time(9, 10))
        duty = self.lead(rehab, staff, slots, group)
        board.add_block(
            rehab, staff["sokolov"], slots["9:10"], TUE, "GROUP_LEAD",
            until=date(2026, 10, 8), group_session=session(group, time(9, 10)),
        )  # fmt: skip
        item = session(group, time(9, 10))
        item.is_active = False

        result = services.save_group_session(rehab, item)

        duty.refresh_from_db()
        assert duty.valid_to == TUE
        # Разовые на будущее убраны, сегодняшний остался.
        dates = InstructorBlock.objects.values_list("date", flat=True)
        assert list(dates) == [TUE]
        assert [str(f) for f in result.freed] == ["Соколов 9:10 (Г)"]

    def test_session_moved_out_of_slot(self, rehab, staff, slots):
        group = new_group(rehab, Section.objects.get(name="ЛФК"), "Г", time(9, 10))
        duty = self.lead(rehab, staff, slots, group, day=WED)
        item = session(group, time(9, 10))

        item.start_time = time(9, 20)  # всё ещё в слоте 9:10–9:40
        assert services.save_group_session(rehab, item).freed == []
        assert InstructorDuty.objects.filter(pk=duty.pk, valid_to=None).exists()

        item.start_time = time(10, 30)
        result = services.save_group_session(rehab, item)

        assert not InstructorDuty.objects.filter(pk=duty.pk).exists()  # начинался завтра
        assert [str(f) for f in result.freed] == ["Соколов 9:10 (Г)"]

    def test_freed_shown_on_screen(self, client, rehab, staff, slots):
        group = new_group(rehab, Section.objects.get(name="ЛФК"), "Г", time(9, 10))
        self.lead(rehab, staff, slots, group)
        item = session(group, time(9, 10))
        client.force_login(rehab)

        html = htmx(client, reverse("scheduling:session_delete", args=[item.pk]), {"confirm": "1"})

        assert "Время 09:10 в архиве" in html
        assert "«Ведение группы» закончилось сегодня" in html and "Соколов 9:10 (Г)" in html


class TestValidation:
    def test_session_past_midnight(self, client, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0))
        client.force_login(rehab)
        data = {"procedure": group.pk, "start_time": "23:50", "duration_min": "30"}

        html = htmx(client, reverse("scheduling:session_add"), data | {"section": group.section_id})

        assert "заканчивается после полуночи" in html
        assert not group.sessions.filter(start_time=time(23, 50)).exists()
        item = session(group, time(9, 0))
        item.duration_min = 15 * 60
        with pytest.raises(ValidationError, match="после полуночи"):
            item.full_clean()

    def test_new_group_past_midnight(self, client, rehab):
        ds = Section.objects.get(name="ДС")
        client.force_login(rehab)
        data = {
            "group-section": ds.pk,
            "group-name": "Ночь",
            "group-default_duration_min": "30",
            "group-times": ["23:45"],
        }
        html = htmx(client, reverse("scheduling:group_add", args=[ds.pk]), data)
        assert "до полуночи: 23:45 + 30 мин" in html
        assert not Procedure.objects.filter(name="Ночь").exists()

    def test_long_name_without_card_label(self, client, rehab):
        ds = Section.objects.get(name="ДС")
        client.force_login(rehab)
        data = {
            "group-section": ds.pk,
            "group-name": "Д" * 70,
            "group-default_duration_min": "30",
            "group-times": ["10:00"],
        }
        html = htmx(client, reverse("scheduling:group_add", args=[ds.pk]), data)
        assert "в названии 70: заполните «как печатается в карте»" in html

        data["group-card_label"] = "Длинная"
        htmx(client, reverse("scheduling:group_add", args=[ds.pk]), data)
        assert Procedure.objects.get(name="Д" * 70).card_label == "Длинная"

    def test_section_name_case_insensitive(self, rehab):
        with pytest.raises(ValidationError, match="Раздел с таким названием уже есть"):
            new_section(rehab, "лфк", SectionBehavior.LFK_GROUP)

    def test_procedure_name_case_insensitive_in_db(self, db):
        Procedure.objects.create(name="Тест", card_label="Т", kind=ProcedureKind.INDIVIDUAL)
        with pytest.raises(IntegrityError), transaction.atomic():
            Procedure.objects.create(name="ТЕСТ", card_label="Т", kind=ProcedureKind.INDIVIDUAL)


class TestAdmin:
    def test_group_is_active_readonly(self, client, admin_user, procedures):
        client.force_login(admin_user)
        group, individual = procedures["group"], procedures["individual"]

        html = client.get(
            reverse("admin:catalog_procedure_change", args=[group.pk])
        ).content.decode()
        assert 'name="is_active"' not in html and "выключают на экране" in html
        html = client.get(
            reverse("admin:catalog_procedure_change", args=[individual.pk])
        ).content.decode()
        assert 'name="is_active"' in html

    def test_section_saved_by_service(self, client, admin_user, procedures):
        client.force_login(admin_user)
        lfk = Section.objects.get(name="ЛФК")

        response = client.post(
            reverse("admin:catalog_section_change", args=[lfk.pk]),
            {"name": "ЛФК-2", "behavior": "LFK_GROUP", "order": "1"},
        )

        assert response.status_code == 302
        lfk.refresh_from_db()
        assert lfk.name == "ЛФК-2" and lfk.history.first().history_user == admin_user


# --- Панель, удаление и архив (решение 82, отзыв владельца) -------------------------------


class TestPanel:
    def test_group_panel_with_highlighted_row(self, client, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Спина-тест", time(9, 0))
        client.force_login(rehab)

        html = client.get(reverse("scheduling:group_edit", args=[group.pk])).content.decode()

        panel, table = html.split('<div id="group-sessions"', 1)
        assert 'id="group-panel" class="side-panel"' in panel
        assert 'value="09:00"' in panel and "Удалить группу" in panel
        assert 'name="group-is_active"' not in panel  # «действует» заменили «Удалить»/«Вернуть»
        assert "table-active" in table and 'hx-swap-oob="true"' in table

    def test_session_edit_error_stays_in_its_row(self, client, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0), time(10, 0))
        client.force_login(rehab)
        late = session(group, time(10, 0))

        html = htmx(
            client,
            reverse("scheduling:session_edit", args=[late.pk]),
            {"edit-start_time": "23:50", "edit-duration_min": "30", "edit-place": ""},
        )

        assert "после полуночи" in html and 'value="23:50"' in html
        late.refresh_from_db()
        assert late.start_time == time(10, 0)

    def test_equipment_panel(self, client, rehab, procedures):
        st = Equipment.objects.get(name="st-150")
        client.force_login(rehab)
        url = reverse("scheduling:equipment_edit", args=[st.pk])

        html = client.get(url).content.decode()
        assert html.lstrip().startswith('<div id="equipment-panel"') and "table-active" in html

        html = htmx(
            client,
            url,
            {
                "eq-window_start": "13:00",
                "eq-window_end": "15:00",
                "eq-step_min": "15",
                "eq-duration_min": "15",
                "eq-capacity": "3",
                "eq-name": "st-150",
                "eq-card_label": "st-150",
            },
        )
        assert "Сохранено" in html
        st.refresh_from_db()
        assert st.capacity == 3


class TestDeleteAndArchive:
    def test_unused_session_deleted(self, client, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0), time(10, 0))
        item = session(group, time(10, 0))
        client.force_login(rehab)
        url = reverse("scheduling:session_delete", args=[item.pk])

        step = htmx(client, url)
        assert "ни разу не использовалось — оно удалится совсем" in step
        assert GroupSession.objects.filter(pk=item.pk).exists()

        html = htmx(client, url, {"confirm": "1"})

        assert "Время 10:00 удалено" in html
        assert not GroupSession.objects.filter(pk=item.pk).exists()
        assert GroupSession.history.filter(id=item.pk, history_type="-").get().history_user == rehab

    def test_used_session_archived_and_restored(self, rehab, doctor, make_program):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0), time(11, 10))
        program = make_program()
        item = add_prescription(doctor, Prescription(program=program, procedure=group))
        late = session(group, time(11, 10))
        services.choose_group_time(doctor, item, late)

        removed = services.delete_group_session(rehab, late)

        assert removed.archived and [p.pk for p in removed.rescheduled.reset] == [item.pk]
        late.refresh_from_db()
        assert not late.is_active
        assert set(item.bookings.values_list("start", flat=True)) == {time(9, 0)}
        with pytest.raises(services.ScheduleError, match="уже в архиве"):
            services.delete_group_session(rehab, late)

        services.restore_group_session(rehab, late)
        late.refresh_from_db()
        assert late.is_active

    def test_unused_group_deleted_with_times(self, client, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0), time(10, 0))
        client.force_login(rehab)
        url = reverse("scheduling:group_delete", args=[group.pk])

        assert "удалится совсем вместе со своими временами" in htmx(client, url)
        html = htmx(client, url, {"confirm": "1"})

        assert "Группа «Г» удалена" in html
        assert not Procedure.objects.filter(pk=group.pk).exists()
        assert not GroupSession.objects.filter(procedure_id=group.pk).exists()
        assert Procedure.history.filter(id=group.pk, history_type="-").get().history_user == rehab

    def test_group_used_by_duty_only_is_archived(self, rehab, staff, slots):
        group = new_group(rehab, Section.objects.get(name="ЛФК"), "Г", time(9, 10))
        board.add_duty(
            rehab, staff["sokolov"], slots["9:10"], WED, "GROUP_LEAD",
            group_session=session(group, time(9, 10)),
        )  # fmt: skip

        removed = services.delete_group(rehab, group)

        assert removed.archived
        group.refresh_from_db()
        assert not group.is_active and Procedure.objects.filter(pk=group.pk).exists()
        assert [str(f) for f in removed.rescheduled.freed] == ["Соколов 9:10 (Г)"]

    def test_archived_group_restored_without_times(self, client, rehab, doctor, make_program):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0))
        add_prescription(doctor, Prescription(program=make_program(), procedure=group))
        services.delete_group(rehab, group)
        client.force_login(rehab)
        archived = session(group, time(9, 0))

        html = htmx(client, reverse("scheduling:session_restore", args=[archived.pk]))
        assert "Сначала верните из архива саму группу" in html

        html = htmx(client, reverse("scheduling:group_restore", args=[group.pk]))
        assert "снова действует" in html and "Удалить группу" in html
        group.refresh_from_db()
        assert group.is_active and not group.sessions.filter(is_active=True).exists()

        htmx(client, reverse("scheduling:session_restore", args=[archived.pk]))
        archived.refresh_from_db()
        assert archived.is_active

    def test_rights(self, client, doctor, make_user, other_department, rehab):
        group = new_group(rehab, Section.objects.get(name="ДС"), "Г", time(9, 0))
        item = session(group, time(9, 0))
        stranger = make_user("stranger", (other_department, Preset.REHAB))
        own = new_group(
            stranger, Section.objects.get(name="ДС"), "Чужая", time(9, 0),
            department=other_department,
        )  # fmt: skip
        client.force_login(doctor)
        for name, pk in (
            ("group_delete", group.pk),
            ("group_restore", group.pk),
            ("session_delete", item.pk),
            ("session_restore", item.pk),
        ):
            assert client.post(reverse(f"scheduling:{name}", args=[pk])).status_code == 403
        with pytest.raises(PermissionDenied):
            services.delete_group(doctor, group)
        client.force_login(rehab)
        url = reverse("scheduling:group_delete", args=[own.pk])
        assert client.post(url, {"confirm": "1"}).status_code == 404
        with pytest.raises(PermissionDenied):
            services.delete_group(rehab, own)
        assert Procedure.objects.filter(pk=own.pk).exists()
