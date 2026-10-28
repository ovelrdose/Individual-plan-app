"""Экран «Справочники»: процедуры, инструкторы, сетка слотов, журнал выгрузок (TZ.md
FR-CAT-7, FR-STF-1, FR-CRD-5, решение 83). Инструкторы и пациенты — вымышленные."""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts.models import Preset
from apps.cards.models import CardExport
from apps.catalog import services
from apps.catalog.models import Equipment, Procedure, ProcedureKind
from apps.catalog.services import search_procedures
from apps.programs.models import Prescription
from apps.programs.services import add_prescription
from apps.scheduling import board_days
from apps.scheduling.domain.model import IssueCode
from apps.scheduling.models import BookingKind

TUE = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def proc_data(name: str, kind: str = ProcedureKind.CARD_ONLY, **changes) -> dict:
    return {
        "proc-name": name,
        "proc-card_label": "",
        "proc-synonyms": ["", "Синоним " + name],
        "proc-kind": kind,
        "proc-default_duration_min": "",
        "proc-place": "",
        "proc-department": "",
    } | changes


class TestScreen:
    def test_tabs_by_rights(self, client, rehab, make_user, department):
        client.force_login(rehab)
        html = client.get(reverse("catalog:index")).content.decode()
        assert "Процедуры" in html and "Инструкторы" in html and "Сетка слотов" in html
        assert "Журнал выгрузок карт" not in html  # ФИО пациентов — право «Пользователи»
        assert client.get(reverse("catalog:index"), {"tab": "exports"}).status_code == 200

        boss = make_user("boss", (department, Preset.ADMIN))
        client.force_login(boss)
        assert "Журнал выгрузок карт" in client.get(reverse("catalog:index")).content.decode()

    def test_doctor_forbidden(self, client, doctor, procedures):
        client.force_login(doctor)
        assert client.get(reverse("catalog:index")).status_code == 403
        pk = procedures["card_only"].pk
        for name in ("procedure_edit", "procedure_delete", "procedure_restore"):
            assert client.post(reverse(f"catalog:{name}", args=[pk])).status_code == 403
        assert client.post(reverse("staff:instructor_add")).status_code == 403

    def test_filter_and_search(self, client, rehab, procedures):
        client.force_login(rehab)
        url = reverse("catalog:index")

        html = client.get(url).content.decode()
        assert "Индивидуальное занятие" in html and "Эрго общая" not in html  # групп нет

        html = client.get(url, {"kind": "CARD_ONLY"}).content.decode()
        assert "Эрготерапевт" in html and "Индивидуальное занятие" not in html

        html = client.get(url, {"q": "консультация эрготерап"}).content.decode()
        assert "Эрготерапевт" in html and "st-150" not in html

        html = client.get(url, {"kind": "GROUPS"}).content.decode()
        assert "Эрго общая" in html and "Групповых занятиях" in html

    def test_slots_tab(self, client, rehab, slots):
        client.force_login(rehab)
        html = client.get(reverse("catalog:index"), {"tab": "slots"}).content.decode()
        assert "9:10" in html and "18:00" in html and "подбор не использует" in html


class TestProcedures:
    def test_create_card_only(self, client, rehab):
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_add"), proc_data("Массаж спины"))

        item = Procedure.objects.get(name="Массаж спины")
        assert "заведена" in html and item.card_label == "Массаж спины"
        assert item.synonyms == ["Синоним Массаж спины"]
        assert item.history.first().history_user == rehab

    def test_pool_only_without_group(self, client, rehab):
        client.force_login(rehab)
        html = htmx(client, reverse("catalog:procedure_add"), proc_data("Вода", "POOL"))
        assert "только «Бассейн» без группы" in html
        htmx(
            client,
            reverse("catalog:procedure_add"),
            proc_data("Вода", "POOL", **{"proc-group_choice": "on"}),
        )
        assert Procedure.objects.get(name="Вода").is_generic_pool

    def test_kind_choices_exclude_groups_and_equipment(self, client, rehab):
        client.force_login(rehab)
        html = client.get(reverse("catalog:procedure_add")).content.decode()
        html = html.split('<div id="procedures-table"')[0]  # окно, без фильтра таблицы
        assert 'value="CARD_ONLY"' in html and 'value="LFK_GROUP"' not in html
        assert 'value="EQUIPMENT"' not in html
        with pytest.raises(services.CatalogError, match="Тренажёр заводится"):
            services.create_procedure(
                rehab, Procedure(name="Т", card_label="Т", kind=ProcedureKind.EQUIPMENT)
            )

    def test_kind_fixed_once_prescribed(self, rehab, doctor, make_program, procedures):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        item.kind = ProcedureKind.INDIVIDUAL
        with pytest.raises(services.CatalogError, match="уже назначали"):
            services.save_procedure(rehab, item)
        item.refresh_from_db()
        item.card_label = "Эрготерапевт (конс.)"
        services.save_procedure(rehab, item)  # подпись — можно

    def test_group_not_edited_here(self, rehab, procedures):
        with pytest.raises(services.CatalogError, match="Групповых занятиях"):
            services.save_procedure(rehab, procedures["group"])
        with pytest.raises(services.CatalogError, match="Групповых занятиях"):
            services.delete_procedure(rehab, procedures["group"])

    def test_other_department_hidden(self, client, rehab, other_department):
        foreign = Procedure.objects.create(
            name="Чужая", card_label="Чужая", kind=ProcedureKind.CARD_ONLY,
            department=other_department,
        )  # fmt: skip
        client.force_login(rehab)
        assert client.get(reverse("catalog:procedure_edit", args=[foreign.pk])).status_code == 404
        with pytest.raises(PermissionDenied):
            services.delete_procedure(rehab, foreign)

    def test_unused_deleted(self, client, rehab):
        item = Procedure.objects.create(name="Лишняя", card_label="Л", kind=ProcedureKind.CARD_ONLY)
        client.force_login(rehab)
        url = reverse("catalog:procedure_delete", args=[item.pk])

        assert "удалится совсем" in htmx(client, url)
        assert "удалена" in htmx(client, url, {"confirm": "1"})
        assert not Procedure.objects.filter(pk=item.pk).exists()
        assert Procedure.history.filter(id=item.pk, history_type="-").get().history_user == rehab

    def test_card_only_used_archived(self, client, rehab, doctor, make_program, procedures):
        item = procedures["card_only"]
        program = make_program()
        add_prescription(doctor, Prescription(program=program, procedure=item))
        client.force_login(rehab)
        url = reverse("catalog:procedure_delete", args=[item.pk])

        assert "строка в карте останется" in htmx(client, url)
        html = htmx(client, url, {"confirm": "1"})

        item.refresh_from_db()
        assert not item.is_active and "В архиве (1)" in html
        assert search_procedures(program.department, "Эрготерапевт") == []
        assert program.prescriptions.filter(procedure=item).exists()
        htmx(client, reverse("catalog:procedure_restore", args=[item.pk]))
        item.refresh_from_db()
        assert item.is_active

    def test_individual_with_current_programs_not_archived(
        self, rehab, doctor, make_program, procedures
    ):
        item = procedures["individual"]
        add_prescription(
            doctor, Prescription(program=make_program(full_name="Держимов Т.Т."), procedure=item)
        )
        with pytest.raises(services.CatalogError, match="Держимов"):
            services.delete_procedure(rehab, item)
        item.refresh_from_db()
        assert item.is_active

    def test_equipment_procedure_archives_equipment(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["equipment"]
        program = make_program()
        prescription = add_prescription(doctor, Prescription(program=program, procedure=item))
        assert prescription.bookings.filter(kind=BookingKind.EQUIPMENT).exists()
        client.force_login(rehab)
        url = reverse("catalog:procedure_delete", args=[item.pk])

        assert "Будет затронуто текущих программ: 1" in htmx(client, url)
        html = htmx(client, url, {"confirm": "1"})

        item.refresh_from_db()
        assert not item.is_active and not item.equipment.is_active
        assert "Расписание пересобрано у программ (1)" in html
        assert not prescription.bookings.exists()
        program.refresh_from_db()
        assert IssueCode.EQUIPMENT_UNPLACED in {i["code"] for i in program.schedule_issues}

        htmx(client, reverse("catalog:procedure_restore", args=[item.pk]))
        item.refresh_from_db()
        assert item.is_active and item.equipment.is_active
        assert prescription.bookings.filter(kind=BookingKind.EQUIPMENT).exists()


class TestExports:
    def test_journal_filter(self, client, make_user, department, make_program, doctor):
        program = make_program(full_name="Выгрузов Тест Тестович")
        other = make_program(full_name="Другов Тест Тестович", history_number="101")
        CardExport.objects.create(program=program, user=doctor)
        CardExport.objects.create(program=other, user=doctor)
        boss = make_user("boss", (department, Preset.ADMIN))
        client.force_login(boss)
        url = reverse("catalog:index")

        html = client.get(url, {"tab": "exports", "q": "выгруз"}).content.decode()
        assert "Выгрузов" in html and "Другов" not in html
        html = client.get(url, {"tab": "exports", "date": "2000-01-01"}).content.decode()
        assert "Выгрузов" not in html and "Выгрузок нет" in html

    def test_rehab_gets_no_journal(self, client, rehab, make_program, doctor):
        CardExport.objects.create(program=make_program(full_name="Скрытов Т.Т."), user=doctor)
        client.force_login(rehab)
        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()
        assert "Скрытов" not in html


class TestEquipmentInGroupSchedule:
    """Тренажёр в «Групповых занятиях»: название, подпись и синонимы в окне; «Удалить» вместо
    галочки (решение 83)."""

    def test_rename_with_procedure(self, client, rehab, procedures):
        st = Equipment.objects.get(name="st-150")
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("scheduling:equipment_edit", args=[st.pk]),
            {
                "eq-name": "ST-150 new",
                "eq-card_label": "st новый",
                "eq-synonyms": ["ST новый"],
                "eq-window_start": "13:00",
                "eq-window_end": "15:00",
                "eq-step_min": "15",
                "eq-duration_min": "15",
                "eq-capacity": "1",
            },
        )

        assert "Сохранено" in html and 'name="eq-is_active"' not in html
        item = procedures["equipment"]
        item.refresh_from_db()
        assert (item.name, item.card_label, item.synonyms) == (
            "ST-150 new",
            "st новый",
            ["ST новый"],
        )

    def test_unused_deleted_with_procedure(self, client, rehab):
        new = Equipment.objects.create(name="Пустой")
        Procedure.objects.create(
            name="Пустой", card_label="Пустой", kind=ProcedureKind.EQUIPMENT, equipment=new
        )
        client.force_login(rehab)
        url = reverse("scheduling:equipment_delete", args=[new.pk])

        assert "удалится совсем" in htmx(client, url)
        assert "удалён" in htmx(client, url, {"confirm": "1"})
        assert not Equipment.objects.filter(pk=new.pk).exists()
        assert not Procedure.objects.filter(name="Пустой").exists()

    def test_used_archived_shown_in_archive(self, client, rehab, doctor, make_program, procedures):
        add_prescription(
            doctor, Prescription(program=make_program(), procedure=procedures["equipment"])
        )
        st = Equipment.objects.get(name="st-150")
        client.force_login(rehab)

        html = htmx(client, reverse("scheduling:equipment_delete", args=[st.pk]), {"confirm": "1"})

        assert "в архиве" in html and "В архиве (1)" in html and "Вернуть из архива" in html
        st.refresh_from_db()
        assert (
            not st.is_active
            and not procedures["equipment"]
            .__class__.objects.get(pk=procedures["equipment"].pk)
            .is_active
        )

    def test_rights(self, client, doctor, procedures):
        st = Equipment.objects.get(name="st-150")
        client.force_login(doctor)
        for name in ("equipment_delete", "equipment_restore"):
            assert client.post(reverse(f"scheduling:{name}", args=[st.pk])).status_code == 403


class TestInstructors:
    def test_create_with_partner(self, client, rehab, staff):
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("staff:instructor_add"),
            {
                "ins-short_name": "Орлов",
                "ins-full_name": "Орлов О.О.",
                "ins-partner": staff["sokolov"].pk,
                "ins-display_order": "3",
            },
        )

        assert "заведён" in html
        orlov = staff["sokolov"].__class__.objects.get(short_name="Орлов")
        staff["sokolov"].refresh_from_db()
        assert orlov.partner == staff["sokolov"] and staff["sokolov"].partner == orlov
        assert orlov.history.first().history_user == rehab

    def test_rights(self, client, doctor, staff):
        client.force_login(doctor)
        pk = staff["sokolov"].pk
        for name in ("instructor_edit", "instructor_delete", "instructor_restore"):
            assert client.post(reverse(f"staff:{name}", args=[pk])).status_code == 403
