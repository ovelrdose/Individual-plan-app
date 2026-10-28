"""Экран «Справочники» — сценарии, которых нет в test_catalog_screen/test_catalog_edges (TZ.md
FR-CAT-7, FR-CRD-5, решение 83): HTMX-ответы окна, архивная процедура в назначениях и в
повторном курсе, журнал выгрузок (дата по местному времени, поиск), сетка слотов — только
просмотр, удаление с автором в журнале. Данные — вымышленные."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Preset, Right
from apps.cards.models import CardExport
from apps.catalog import services
from apps.catalog.models import InstructorSlot, Procedure, ProcedureKind
from apps.programs.models import Prescription
from apps.programs.services import add_prescription, link_previous
from apps.scheduling import board_days

TUE = date(2026, 10, 6)
MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def boss(make_user, department):
    return make_user("boss", (department, Preset.ADMIN))


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def proc_data(name: str, kind: str = ProcedureKind.CARD_ONLY, **changes) -> dict:
    return {
        "proc-name": name,
        "proc-card_label": "",
        "proc-synonyms": [""],
        "proc-kind": kind,
        "proc-default_duration_min": "",
        "proc-place": "",
        "proc-department": "",
    } | changes


class TestHtmxWindow:
    """Ошибка формы — 200 в том же окне; таблица всегда приходит через hx-swap-oob."""

    def test_form_error_in_window_with_table_oob(self, client, rehab, procedures):
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_add"), proc_data(""))

        assert html.lstrip().startswith('<div id="procedure-panel"')
        assert "Обязательное поле" in html
        assert '<div id="procedures-table" data-panel-table hx-swap-oob="true">' in html
        assert not Procedure.objects.filter(name="").exists()

    def test_edit_error_keeps_saved_values(self, client, rehab, procedures):
        item = procedures["card_only"]
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("catalog:procedure_edit", args=[item.pk]),
            proc_data("Эрготерапевт", **{"proc-default_duration_min": "-5"}),
        )

        assert 'id="procedure-panel"' in html and "hx-swap-oob" in html
        assert "invalid-feedback" in html and 'value="-5"' in html  # введённое не теряется
        item.refresh_from_db()
        assert item.default_duration_min is None

    def test_zero_duration_refused(self, client, rehab):
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("catalog:procedure_add"),
            proc_data("Нулевая", **{"proc-default_duration_min": "0"}),
        )

        assert "invalid-feedback" in html
        assert not Procedure.objects.filter(name="Нулевая").exists()

    def test_success_marks_row_in_oob_table(self, client, rehab):
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_add"), proc_data("Тестовая строка"))

        created = Procedure.objects.get(name="Тестовая строка")
        assert created.card_label == "Тестовая строка"  # пустая подпись — как название
        table = html[html.index('id="procedures-table"') :]
        assert "Тестовая строка" in table and "table-active" in table

    def test_instructor_form_error_with_table_oob(self, client, rehab, staff):
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("staff:instructor_add"),
            {"ins-short_name": "", "ins-full_name": "", "ins-partner": "", "ins-display_order": ""},
        )

        assert html.lstrip().startswith('<div id="instructor-panel"')
        assert '<div id="shift-calendar" data-panel-table hx-swap-oob="true">' in html
        assert "Обязательное поле" in html


class TestArchivedProcedureNotChosenAgain:
    """В архиве — у существующих назначений остаётся, выбрать заново нельзя (FR-CAT-7)."""

    def test_add_prescription_with_archived_refused(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["card_only"]
        first = make_program()
        add_prescription(doctor, Prescription(program=first, procedure=item))
        services.delete_procedure(rehab, item)
        second = make_program(history_number="200", full_name="Вторых Тест Тестович")
        client.force_login(doctor)

        response = client.post(
            reverse("programs:prescription_add", args=[second.pk]),
            {"procedure": item.pk, "per_day": "1", "in_card": "on"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert "Выберите корректный вариант" in response.content.decode()
        assert not second.prescriptions.exists()

    def test_other_row_cannot_switch_to_archived(
        self, client, rehab, doctor, make_program, procedures
    ):
        archived, other = procedures["card_only"], procedures["individual"]
        program = make_program()
        add_prescription(doctor, Prescription(program=program, procedure=archived))
        row = add_prescription(doctor, Prescription(program=program, procedure=other))
        services.delete_procedure(rehab, archived)
        client.force_login(doctor)

        client.post(
            reverse("programs:prescription_edit", args=[program.pk, row.pk]),
            {"edit-procedure": archived.pk, "edit-per_day": "1", "edit-in_card": "on"},
            HTTP_HX_REQUEST="true",
        )

        row.refresh_from_db()
        assert row.procedure_id == other.pk

    def test_archived_row_shows_hint_in_panel(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["card_only"]
        program = make_program()
        row = add_prescription(doctor, Prescription(program=program, procedure=item))
        services.delete_procedure(rehab, item)
        client.force_login(doctor)

        html = client.get(
            reverse("programs:prescription_edit", args=[program.pk, row.pk])
        ).content.decode()

        assert "архив" in html.lower()

    @pytest.mark.parametrize("key", ["card_only", "individual"])
    def test_previous_plan_does_not_bring_archived(
        self, rehab, doctor, make_program, procedures, key
    ):
        item = procedures[key]
        old = make_program(start_date=date(2026, 9, 1), history_number="1")
        add_prescription(doctor, Prescription(program=old, procedure=item))
        services.delete_procedure(rehab, item)
        new = make_program(start_date=TUE, history_number="2")

        link_previous(doctor, new, old, copy=True)

        assert not new.prescriptions.filter(procedure__is_active=False).exists()
        # Строка не теряется: приходит нераспознанной, врач заменяет процедуру сам.
        row = new.prescriptions.get()
        assert row.procedure is None and row.raw_text == item.name

    def test_archived_procedure_edited_stays_archived(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        services.delete_procedure(rehab, item)
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("catalog:procedure_edit", args=[item.pk]),
            proc_data("Эрготерапевт", **{"proc-card_label": "Эрготерапевт (конс.)"}),
        )

        item.refresh_from_db()
        assert item.card_label == "Эрготерапевт (конс.)" and not item.is_active
        assert "Вернуть из архива" in html

    def test_restore_from_window_returns_to_search(
        self, client, rehab, doctor, department, make_program, procedures
    ):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        services.delete_procedure(rehab, item)
        assert item not in services.search_procedures(department, "Эрготерап")
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_restore", args=[item.pk]))

        item.refresh_from_db()
        assert item.is_active and "снова действует" in html
        assert item in services.search_procedures(department, "Эрготерап")
        table = html[html.index('id="procedures-table"') :]
        assert "В архиве (" not in table


class TestDeleteHistory:
    """Неиспользованная запись удаляется совсем — автор остаётся в журнале (решение 83)."""

    def test_procedure_delete_logged_with_author(self, client, rehab):
        item = services.create_procedure(
            rehab,
            Procedure(name="Временная", kind=ProcedureKind.CARD_ONLY),
        )
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_delete", args=[item.pk]), {"confirm": "1"})

        assert "удалена" in html
        last = Procedure.history.filter(id=item.pk).order_by("-history_date").first()
        assert last.history_type == "-" and last.history_user == rehab

    def test_archive_logged_with_author(self, rehab, doctor, make_program, procedures):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))

        services.delete_procedure(rehab, item)

        last = item.history.order_by("-history_date").first()
        assert (last.history_type, last.is_active, last.history_user) == ("~", False, rehab)


class TestSlotsReadOnly:
    def test_slots_tab_has_no_edit_controls(self, client, rehab, slots):
        client.force_login(rehab)

        html = client.get(reverse("catalog:index"), {"tab": "slots"}).content.decode()

        body = html[html.index("Сетка одна на центр") :]
        assert "hx-post" not in body and 'method="post"' not in body
        # По порядку времени, вечерний помечен.
        assert body.index("9:10") < body.index("11:10") < body.index("18:00")
        assert "подбор не использует" in body
        assert InstructorSlot.objects.count() == 5

    def test_doctor_cannot_open_slots(self, client, doctor, slots):
        client.force_login(doctor)
        assert client.get(reverse("catalog:index"), {"tab": "slots"}).status_code == 403


class TestExportJournal:
    """FR-CRD-5: пользователь, время, программа; фильтр по дате, поиск по ФИО."""

    def export(self, program, user, moment: datetime) -> CardExport:
        item = CardExport.objects.create(program=program, user=user)
        CardExport.objects.filter(pk=item.pk).update(created_at=moment)
        return item

    def test_date_filter_by_local_day(self, client, boss, doctor, make_program):
        # 22:30 UTC 06.10 — это 01:30 07.10 по Москве: в журнале — 07.10.
        program = make_program(full_name="Полночев Тест Тестович")
        self.export(program, doctor, datetime(2026, 10, 6, 22, 30, tzinfo=ZoneInfo("UTC")))
        client.force_login(boss)
        url = reverse("catalog:index")

        on_7th = client.get(url, {"tab": "exports", "date": "2026-10-07"}).content.decode()
        on_6th = client.get(url, {"tab": "exports", "date": "2026-10-06"}).content.decode()

        assert "Полночев" in on_7th and "07.10.2026 01:30" in on_7th
        assert "Полночев" not in on_6th

    def test_columns_user_time_program(self, client, boss, doctor, make_program):
        program = make_program(full_name="Колонков Тест Тестович")
        self.export(program, doctor, datetime(2026, 10, 6, 10, 5, tzinfo=MSK))
        client.force_login(boss)

        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()

        row = html[html.index("06.10.2026 10:05") :]
        row = row[: row.index("</tr>")]
        assert "Колонков Тест Тестович" in row
        assert reverse("programs:detail", args=[program.pk]) in row
        assert "ОМР № 4" in row
        assert str(doctor) in row

    @pytest.mark.parametrize("query", ["поиСКОВ", "Анна", "Поискова Анна"])
    def test_search_by_full_name_any_part_any_case(self, client, boss, doctor, make_program, query):
        self.export(make_program(full_name="Поискова Анна Петровна"), doctor, timezone.now())
        self.export(
            make_program(full_name="Другая Вера Ивановна", history_number="2"),
            doctor,
            timezone.now(),
        )
        client.force_login(boss)

        html = client.get(reverse("catalog:index"), {"tab": "exports", "q": query}).content
        html = html.decode()

        assert "Поискова Анна Петровна" in html and "Другая Вера" not in html

    def test_newest_first_and_deleted_user_shown_as_dash(
        self, client, boss, make_user, department, make_program
    ):
        gone = make_user("gone", (department, Preset.DOCTOR))
        program = make_program(full_name="Порядков Тест Тестович")
        self.export(program, gone, datetime(2026, 10, 1, 9, 0, tzinfo=MSK))
        self.export(program, boss, datetime(2026, 10, 5, 9, 0, tzinfo=MSK))
        CardExport.objects.filter(user=gone).update(user=None)
        client.force_login(boss)

        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()

        assert html.index("05.10.2026 09:00") < html.index("01.10.2026 09:00")
        older = html[html.index("01.10.2026 09:00") :]
        assert "<td>—</td>" in older[: older.index("</tr>")]

    def test_users_right_sees_other_departments(
        self, client, make_user, department, other_department
    ):
        """Право «Пользователи» видит все отделения (решение 79) — и журнал тоже."""
        from apps.programs.models import Program
        from apps.programs.services import save_program

        stranger = make_user("stranger", (other_department, Preset.DOCTOR), short_name="Д")
        program = save_program(
            stranger,
            Program(
                department=other_department,
                full_name="Чужова Тест Тестовна",
                history_number="1",
                room="1",
                shrm=3,
                attending_doctor=stranger,
                start_date=TUE,
            ),
            end_date_changed=False,
        )
        CardExport.objects.create(program=program, user=stranger)
        clerk = make_user("clerk", (department, Preset.DOCTOR), rights=[Right.USERS])
        client.force_login(clerk)

        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()

        assert "Чужова" in html

    def test_anonymous_redirected(self, client):
        response = client.get(reverse("catalog:index"), {"tab": "exports"})
        assert response.status_code == 302 and "/login/" in response["Location"]


class TestInstructorPartner:
    """Напарник 2/2 — симметрично (FR-STF-1): смена пары разрывает прежнюю у обоих."""

    def test_reassign_partner_breaks_old_pair(self, client, rehab, staff):
        volkov, lebedeva, sokolov = staff["volkov"], staff["lebedeva"], staff["sokolov"]
        client.force_login(rehab)

        htmx(
            client,
            reverse("staff:instructor_edit", args=[volkov.pk]),
            {
                "ins-short_name": "Волков",
                "ins-full_name": "Волков В.В.",
                "ins-partner": sokolov.pk,
                "ins-display_order": "0",
            },
        )

        for item in staff.values():
            item.refresh_from_db()
        assert volkov.partner == sokolov and sokolov.partner == volkov
        assert lebedeva.partner is None
        assert lebedeva.history.first().history_user == rehab

    def test_clear_partner_from_window(self, client, rehab, staff):
        volkov, lebedeva = staff["volkov"], staff["lebedeva"]
        client.force_login(rehab)

        htmx(
            client,
            reverse("staff:instructor_edit", args=[lebedeva.pk]),
            {
                "ins-short_name": "Лебедева",
                "ins-full_name": "Лебедева Л.Л.",
                "ins-partner": "",
                "ins-display_order": "0",
            },
        )

        volkov.refresh_from_db()
        lebedeva.refresh_from_db()
        assert volkov.partner is None and lebedeva.partner is None

    def test_other_department_rehab_sees_same_instructors(
        self, client, make_user, other_department, staff
    ):
        """Инструкторы общие для центра: раздел один на всех с правом «Инструкторы»."""
        other = make_user("other-rehab", (other_department, Preset.REHAB))
        client.force_login(other)

        html = client.get(reverse("staff:shifts")).content.decode()

        assert "Волков" in html and "Соколов" in html


@pytest.mark.parametrize(
    ("user_fixture", "status"),
    [("admin_user", 200), ("rehab", 200), ("doctor", 403)],
)
def test_index_access_matrix(client, request, user_fixture, status):
    client.force_login(request.getfixturevalue(user_fixture))
    for tab in ("procedures", "slots"):
        assert client.get(reverse("catalog:index"), {"tab": tab}).status_code == status


def test_slot_time_shown_without_leading_zero(client, rehab, db):
    InstructorSlot.objects.create(start=time(9, 10), end=time(9, 40))
    client.force_login(rehab)
    html = client.get(reverse("catalog:index"), {"tab": "slots"}).content.decode()
    assert "<td>9:10</td>" in html and "<td>9:40</td>" in html


def test_duplicate_name_rejected_by_database(db):
    """Два одновременных «Сохранить» с одним названием: второе отклоняет база, без учёта
    регистра и для общих процедур (department = NULL)."""
    from django.db import IntegrityError, transaction

    Procedure.objects.create(name="Дублев", card_label="Дублев", kind=ProcedureKind.CARD_ONLY)

    with pytest.raises(IntegrityError), transaction.atomic():
        Procedure.objects.create(name="дублев", card_label="д", kind=ProcedureKind.CARD_ONLY)
