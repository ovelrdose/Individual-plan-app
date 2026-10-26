"""Экран «Справочники» — граничные случаи и права (TZ.md FR-CAT-7, FR-CRD-5, решение 83).

Сегодня — вторник 06.10.2026. Пациенты и процедуры — вымышленные.
"""

from datetime import date
from io import BytesIO

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Preset, Right
from apps.cards.models import CardExport
from apps.cards.services import export_card
from apps.catalog import services
from apps.catalog.models import Equipment, Procedure, ProcedureKind
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.programs.models import Prescription, WithdrawalReason
from apps.programs.services import add_prescription, cancel_prescription, withdraw
from apps.scheduling import board_days
from apps.scheduling.models import BookingKind
from tests.sheets import make_sheet

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
        "proc-synonyms": [""],
        "proc-kind": kind,
        "proc-default_duration_min": "",
        "proc-place": "",
        "proc-department": "",
    } | changes


def new_procedure(user, name: str, kind=ProcedureKind.CARD_ONLY, **fields) -> Procedure:
    return services.create_procedure(user, Procedure(name=name, kind=kind, **fields))


def import_sheet(doctor, department, rows):
    return import_prescription_sheet(
        doctor,
        department,
        parse_sheet(make_sheet(rows=rows)),
        attending_doctor=doctor,
        force=True,
        today=TUE,
    )


class TestCreate:
    @pytest.mark.parametrize(
        ("kind", "flags", "check"),
        [
            (ProcedureKind.INDIVIDUAL, {}, lambda p: p.kind == ProcedureKind.INDIVIDUAL),
            (ProcedureKind.CARD_ONLY, {}, lambda p: not p.evening_individual),
            (
                ProcedureKind.CARD_ONLY,
                {"proc-evening_individual": "on"},
                lambda p: p.evening_individual,
            ),
            (ProcedureKind.POOL, {"proc-group_choice": "on"}, lambda p: p.is_generic_pool),
        ],
    )
    def test_kinds_and_flags(self, client, rehab, kind, flags, check):
        client.force_login(rehab)

        html = htmx(
            client,
            reverse("catalog:procedure_add"),
            proc_data(
                "Новая процедура",
                kind,
                **{"proc-synonyms": ["  Синоним один ", "", "Синоним два"]},
                **{"proc-default_duration_min": "20", "proc-place": "зал 2"},
                **flags,
            ),
        )

        assert "заведена" in html
        item = Procedure.objects.get(name="Новая процедура")
        assert check(item) and item.is_active and item.section is None
        assert item.synonyms == ["Синоним один", "Синоним два"]
        assert (item.default_duration_min, item.place) == (20, "зал 2")

    @pytest.mark.parametrize(
        ("kind", "flags", "text"),
        [
            (ProcedureKind.CARD_ONLY, {"proc-group_choice": "on"}, "только у бассейна"),
            (ProcedureKind.INDIVIDUAL, {"proc-evening_individual": "on"}, "Только строка"),
        ],
    )
    def test_wrong_flags_rejected(self, client, rehab, kind, flags, text):
        client.force_login(rehab)
        html = htmx(client, reverse("catalog:procedure_add"), proc_data("Флаги", kind, **flags))
        assert text in html
        assert not Procedure.objects.filter(name="Флаги").exists()

    @pytest.mark.parametrize("kind", ["LFK_GROUP", "DS_GROUP", "EQUIPMENT", "чепуха", ""])
    def test_group_and_equipment_kinds_rejected_in_form(self, client, rehab, kind):
        client.force_login(rehab)
        html = htmx(client, reverse("catalog:procedure_add"), proc_data("Хитрая", kind))
        assert "Выберите корректный вариант" in html or "Обязательное поле" in html
        assert not Procedure.objects.filter(name="Хитрая").exists()

    def test_empty_name(self, client, rehab):
        client.force_login(rehab)
        html = htmx(client, reverse("catalog:procedure_add"), proc_data("   "))
        assert "Обязательное поле" in html
        assert not Procedure.objects.exists()

    @pytest.mark.parametrize("name", ["эрго ОБЩАЯ", "ST-150", "  ЭРГОТЕРАПЕВТ  "])
    def test_duplicates_ignore_case_with_groups_and_equipment(
        self, client, rehab, procedures, name
    ):
        client.force_login(rehab)
        before = Procedure.objects.count()

        html = htmx(client, reverse("catalog:procedure_add"), proc_data(name))

        assert "уже есть" in html
        assert Procedure.objects.count() == before

    def test_duplicate_of_archived_refused(self, rehab, doctor, make_program, procedures):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        services.delete_procedure(rehab, item)
        with pytest.raises(services.CatalogError, match="уже есть"):
            new_procedure(rehab, "эрготерапевт")

    def test_rename_into_duplicate_refused(self, client, rehab, procedures):
        item = procedures["card_only"]
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("catalog:procedure_edit", args=[item.pk]),
            proc_data("индивидуальное ЗАНЯТИЕ"),
        )
        assert "уже есть" in html
        item.refresh_from_db()
        assert item.name == "Эрготерапевт"

    def test_department_procedure_only_for_own_department(
        self, client, rehab, department, other_department
    ):
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("catalog:procedure_add"),
            proc_data("Чужая", **{"proc-department": other_department.pk}),
        )
        assert "Выберите корректный вариант" in html
        assert not Procedure.objects.filter(name="Чужая").exists()
        with pytest.raises(PermissionDenied):
            new_procedure(rehab, "Чужая", department=other_department)

        htmx(
            client,
            reverse("catalog:procedure_add"),
            proc_data("Своя", **{"proc-department": department.pk}),
        )
        assert Procedure.objects.get(name="Своя").department == department

    def test_equipment_kind_fixed(self, rehab, procedures):
        item = procedures["equipment"]
        item.kind = ProcedureKind.CARD_ONLY
        with pytest.raises(services.CatalogError, match="тренажёра не меняется"):
            services.save_procedure(rehab, item)

    def test_unused_kind_may_change(self, rehab):
        item = new_procedure(rehab, "Пока строка")
        item.kind = ProcedureKind.INDIVIDUAL
        services.save_procedure(rehab, item)
        item.refresh_from_db()
        assert item.kind == ProcedureKind.INDIVIDUAL


class TestSheetRecognition:
    """Новая процедура сразу распознаётся в листе назначений, архивная — нет (FR-CAT-7)."""

    def test_new_procedure_recognized_by_synonym(self, rehab, doctor, department, procedures):
        new_procedure(
            rehab,
            "Механотерапия кисти",
            ProcedureKind.CARD_ONLY,
            card_label="Механотер.",
            synonyms=["Механотерапия"],
        )

        program = import_sheet(
            doctor, department, [("06.10.26", "Механотерапия кисти 20 мин 1 р/д е/д", "")]
        )

        item = program.prescriptions.get()
        assert item.procedure.name == "Механотерапия кисти"
        assert item.label == "Механотерапия кисти"

    def test_new_individual_recognized(self, rehab, doctor, department, procedures):
        new_procedure(rehab, "Кинезиотерапия", ProcedureKind.INDIVIDUAL, default_duration_min=30)

        program = import_sheet(
            doctor, department, [("06.10.26", "Кинезиотерапия 30 мин 1 р/д е/д", "")]
        )

        assert program.prescriptions.get().procedure.kind == ProcedureKind.INDIVIDUAL

    def test_archived_not_recognized(self, rehab, doctor, department, make_program, procedures):
        item = new_procedure(rehab, "Механотерапия кисти")
        add_prescription(
            doctor, Prescription(program=make_program(history_number="900"), procedure=item)
        )
        services.delete_procedure(rehab, item)

        program = import_sheet(
            doctor, department, [("06.10.26", "Механотерапия кисти 20 мин 1 р/д е/д", "")]
        )

        assert program.prescriptions.get().procedure is None  # нераспознанная строка

    def test_other_department_procedure_not_recognized(
        self, admin_user, doctor, department, other_department, procedures
    ):
        new_procedure(admin_user, "Механотерапия кисти", department=other_department)

        program = import_sheet(
            doctor, department, [("06.10.26", "Механотерапия кисти 20 мин 1 р/д е/д", "")]
        )

        assert program.prescriptions.get().procedure is None


class TestArchive:
    def test_card_only_archived_still_printed(
        self, rehab, doctor, make_program, procedures, department
    ):
        item = procedures["card_only"]
        program = make_program(shrm=3)
        add_prescription(doctor, Prescription(program=program, procedure=item))

        removed = services.delete_procedure(rehab, item)

        assert removed.archived
        _name, content = export_card(doctor, program)
        sheet = load_workbook(BytesIO(content)).active
        assert sheet["A18"].value == "Эрготерапевт"

    def test_individual_only_finished_programs_archived(
        self, rehab, doctor, make_program, procedures
    ):
        item = procedures["individual"]
        add_prescription(
            doctor,
            Prescription(program=make_program(start_date=date(2026, 9, 1)), procedure=item),
        )

        assert services.delete_procedure(rehab, item).archived

    def test_individual_cancelled_prescription_does_not_hold(
        self, rehab, doctor, make_program, procedures
    ):
        item = procedures["individual"]
        prescription = add_prescription(
            doctor, Prescription(program=make_program(), procedure=item)
        )
        cancel_prescription(doctor, prescription, TUE)  # с сегодня занятий нет

        assert services.delete_procedure(rehab, item).archived

    def test_individual_withdrawn_patient_does_not_hold(
        self, rehab, doctor, make_program, procedures
    ):
        item = procedures["individual"]
        program = make_program()
        add_prescription(doctor, Prescription(program=program, procedure=item))
        withdraw(rehab, program, TUE, WithdrawalReason.REFUSAL)

        assert services.delete_procedure(rehab, item).archived

    def test_individual_future_program_holds(self, rehab, doctor, make_program, procedures):
        item = procedures["individual"]
        add_prescription(
            doctor,
            Prescription(
                program=make_program(start_date=date(2026, 10, 12), full_name="Будущев Т.Т."),
                procedure=item,
            ),
        )
        with pytest.raises(services.CatalogError, match="Будущев"):
            services.delete_procedure(rehab, item)

    def test_generic_pool_with_current_program_refused(self, client, rehab, doctor, make_program):
        pool = new_procedure(rehab, "Бассейн", ProcedureKind.POOL, group_choice=True)
        add_prescription(
            doctor, Prescription(program=make_program(full_name="Плавцов Т.Т."), procedure=pool)
        )
        client.force_login(rehab)

        html = htmx(client, reverse("catalog:procedure_delete", args=[pool.pk]), {"confirm": "1"})

        assert "Плавцов" in html and "назначено текущим пациентам (1)" in html
        pool.refresh_from_db()
        assert pool.is_active

    def test_evening_individual_with_current_program_refused(self, rehab, doctor, make_program):
        moto = new_procedure(rehab, "Мото-Т", evening_individual=True)
        add_prescription(
            doctor, Prescription(program=make_program(full_name="Вечеров Т.Т."), procedure=moto)
        )
        with pytest.raises(services.CatalogError, match="Вечеров"):
            services.delete_procedure(rehab, moto)
        moto.refresh_from_db()
        assert moto.is_active

    def test_many_holders_listed_briefly(self, rehab, doctor, make_program, procedures):
        item = procedures["individual"]
        for number in range(12):
            add_prescription(
                doctor,
                Prescription(
                    program=make_program(
                        full_name=f"Пациент{number:02} Т.Т.", history_number=f"{number}"
                    ),
                    procedure=item,
                ),
            )
        with pytest.raises(services.CatalogError, match=r"\(12\).*и ещё 2"):
            services.delete_procedure(rehab, item)

    def test_already_archived(self, rehab, doctor, make_program, procedures):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        services.delete_procedure(rehab, item)
        item.refresh_from_db()
        with pytest.raises(services.CatalogError, match="уже в архиве"):
            services.delete_procedure(rehab, item)

    def test_archived_hidden_from_prescription_search_and_restored(
        self, client, rehab, doctor, make_program, procedures, department
    ):
        item = procedures["card_only"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        services.delete_procedure(rehab, item)
        client.force_login(doctor)
        url = reverse("catalog:procedure_search")
        assert "Эрготерапевт" not in client.get(url, {"q": "эрго"}).content.decode()

        services.restore_procedure(rehab, item)

        assert "Эрготерапевт" in client.get(url, {"q": "эрготерап"}).content.decode()


class TestEquipmentArchive:
    def test_archive_and_restore_return_bookings(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["equipment"]
        programs = [
            make_program(full_name=f"Тренажёров{n} Т.Т.", history_number=str(n)) for n in (1, 2)
        ]
        prescriptions = [
            add_prescription(doctor, Prescription(program=p, procedure=item)) for p in programs
        ]
        future = [
            set(
                p.bookings.filter(kind=BookingKind.EQUIPMENT, date__gte=TUE).values_list(
                    "date", "start"
                )
            )
            for p in prescriptions
        ]
        assert all(future)
        past = [p.bookings.filter(date__lt=TUE).count() for p in prescriptions]
        st = item.equipment
        client.force_login(rehab)

        html = htmx(client, reverse("scheduling:equipment_delete", args=[st.pk]), {"confirm": "1"})

        assert "в архиве" in html
        for prescription, kept in zip(prescriptions, past, strict=True):
            assert not prescription.bookings.filter(date__gte=TUE).exists()
            assert prescription.bookings.filter(date__lt=TUE).count() == kept  # прошлое остаётся
        assert services.search_procedures(programs[0].department, "st") == []

        htmx(client, reverse("scheduling:equipment_restore", args=[st.pk]))

        st.refresh_from_db()
        item.refresh_from_db()
        assert st.is_active and item.is_active
        for prescription, dates in zip(prescriptions, future, strict=True):
            again = set(prescription.bookings.filter(date__gte=TUE).values_list("date", "start"))
            assert {day for day, _ in again} == {day for day, _ in dates}

    def test_equipment_archived_in_group_screen_shown_in_catalog_archive(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["equipment"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        from apps.scheduling.services import delete_equipment

        delete_equipment(rehab, item.equipment)
        client.force_login(rehab)

        html = client.get(reverse("catalog:index"), {"kind": "EQUIPMENT"}).content.decode()

        assert "В архиве (1)" in html and "st-150" in html
        htmx(client, reverse("catalog:procedure_restore", args=[item.pk]))
        assert Equipment.objects.get(pk=item.equipment_id).is_active

    def test_rename_equipment_to_existing_procedure_refused(self, client, rehab, procedures):
        st = Equipment.objects.get(name="st-150")
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("scheduling:equipment_edit", args=[st.pk]),
            {
                "eq-name": "эрготерапевт",
                "eq-card_label": "",
                "eq-synonyms": [""],
                "eq-window_start": "13:00",
                "eq-window_end": "15:00",
                "eq-step_min": "15",
                "eq-duration_min": "15",
                "eq-capacity": "1",
            },
        )
        assert "уже есть" in html
        st.refresh_from_db()
        assert st.name == "st-150"


class TestRights:
    @pytest.mark.parametrize("name", ["procedure_edit", "procedure_delete", "procedure_restore"])
    def test_doctor_post_changes_nothing(
        self, client, doctor, rehab, make_program, procedures, name
    ):
        item = procedures["card_only"]
        if name == "procedure_restore":
            add_prescription(doctor, Prescription(program=make_program(), procedure=item))
            services.delete_procedure(rehab, item)
        item.refresh_from_db()
        before = (item.name, item.is_active)
        client.force_login(doctor)

        response = client.post(
            reverse(f"catalog:{name}", args=[item.pk]), proc_data("Взлом") | {"confirm": "1"}
        )

        assert response.status_code == 403
        item.refresh_from_db()
        assert (item.name, item.is_active) == before

    def test_doctor_add_forbidden(self, client, doctor):
        client.force_login(doctor)
        assert client.post(reverse("catalog:procedure_add"), proc_data("Взлом")).status_code == 403
        assert not Procedure.objects.filter(name="Взлом").exists()

    def test_anonymous_redirected(self, client, procedures):
        item = procedures["card_only"]
        response = client.post(
            reverse("catalog:procedure_delete", args=[item.pk]), {"confirm": "1"}
        )
        assert response.status_code == 302 and "/login/" in response["Location"]
        assert Procedure.objects.filter(pk=item.pk).exists()
        assert client.get(reverse("catalog:index")).status_code == 302

    def test_other_department_post_404(self, client, rehab, other_department):
        foreign = Procedure.objects.create(
            name="Чужая", card_label="Чужая", kind=ProcedureKind.CARD_ONLY,
            department=other_department,
        )  # fmt: skip
        client.force_login(rehab)
        for name in ("procedure_edit", "procedure_delete", "procedure_restore"):
            response = client.post(
                reverse(f"catalog:{name}", args=[foreign.pk]), proc_data("X") | {"confirm": 1}
            )
            assert response.status_code == 404
        foreign.refresh_from_db()
        assert foreign.name == "Чужая" and foreign.is_active
        html = client.get(reverse("catalog:index"), {"q": "Чужая"}).content.decode()
        assert "Ничего не найдено" in html

    def test_users_right_only_sees_journal(self, client, make_user, department):
        clerk = make_user("clerk", (department, Preset.DOCTOR), rights=[Right.USERS])
        client.force_login(clerk)
        html = client.get(reverse("catalog:index")).content.decode()
        assert "Журнал выгрузок карт" in html
        assert client.post(reverse("catalog:procedure_add"), proc_data("X")).status_code == 403
        assert client.get(reverse("catalog:index"), {"tab": "procedures"}).status_code == 200

    def test_doctor_has_no_menu_item(self, client, doctor, rehab):
        client.force_login(doctor)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert reverse("catalog:index") not in html
        client.force_login(rehab)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert reverse("catalog:index") in html


class TestExportJournal:
    def test_card_download_logged_and_visible_to_users_right(
        self, client, make_user, department, doctor, make_program, procedures
    ):
        program = make_program(full_name="Журналов Тест Тестович")
        Prescription.objects.create(program=program, procedure=procedures["card_only"])
        client.force_login(doctor)
        assert client.get(reverse("cards:download", args=[program.pk])).status_code == 200
        assert CardExport.objects.get().user == doctor

        boss = make_user("boss", (department, Preset.ADMIN))
        client.force_login(boss)
        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()
        assert "Журналов" in html

    def test_superuser_sees_all_departments(
        self, client, admin_user, make_user, other_department, make_program, doctor
    ):
        stranger = make_user("stranger", (other_department, Preset.DOCTOR), short_name="Д")
        from apps.programs.models import Program
        from apps.programs.services import save_program

        program = save_program(
            stranger,
            Program(
                department=other_department,
                full_name="Чужев Тест Тестович",
                history_number="1",
                room="1",
                shrm=3,
                attending_doctor=stranger,
                start_date=TUE,
            ),
            end_date_changed=False,
        )
        CardExport.objects.create(program=program, user=stranger)
        client.force_login(admin_user)
        html = client.get(reverse("catalog:index"), {"tab": "exports"}).content.decode()
        assert "Чужев" in html

    @pytest.mark.parametrize("value", ["2026-13-45", "вчера", ""])
    def test_bad_date_filter_ignored(self, client, make_user, department, value):
        boss = make_user("boss", (department, Preset.ADMIN))
        client.force_login(boss)
        response = client.get(reverse("catalog:index"), {"tab": "exports", "date": value})
        assert response.status_code == 200

    def test_rehab_journal_hidden_everywhere(self, client, rehab, make_program, doctor):
        CardExport.objects.create(program=make_program(full_name="Секретов Т.Т."), user=doctor)
        client.force_login(rehab)
        for params in ({"tab": "exports"}, {"tab": "exports", "q": "Секретов"}):
            html = client.get(reverse("catalog:index"), params).content.decode()
            # Вкладки журнала нет — открывается «Процедуры», ФИО пациентов не видно.
            assert "Журнал выгрузок" not in html
            assert "Секретов" not in html.replace('value="Секретов"', "")


class TestArchivedInProgram:
    """Назначение с процедурой в архиве остаётся у пациента — его можно править (FR-CAT-7)."""

    @pytest.mark.parametrize("key", ["card_only", "equipment"])
    def test_edit_prescription_with_archived_procedure(
        self, client, rehab, doctor, make_program, procedures, key
    ):
        item = procedures[key]
        program = make_program()
        prescription = add_prescription(doctor, Prescription(program=program, procedure=item))
        services.delete_procedure(rehab, item)
        client.force_login(doctor)

        client.post(
            reverse("programs:prescription_edit", args=[program.pk, prescription.pk]),
            {
                "edit-procedure": item.pk,
                "edit-per_day": "1",
                "edit-cancel_date": "2026-10-09",
                "edit-in_card": "on",
            },
            HTTP_HX_REQUEST="true",
        )

        prescription.refresh_from_db()
        assert prescription.cancel_date == date(2026, 10, 9)
        assert prescription.procedure_id == item.pk  # архивная осталась у своего назначения
        item.refresh_from_db()
        assert not item.is_active

    def test_cancel_button_works_with_archived_procedure(
        self, client, rehab, doctor, make_program, procedures
    ):
        item = procedures["equipment"]
        program = make_program()
        prescription = add_prescription(doctor, Prescription(program=program, procedure=item))
        services.delete_procedure(rehab, item)
        client.force_login(doctor)

        client.post(
            reverse("programs:prescription_cancel", args=[program.pk, prescription.pk]),
            HTTP_HX_REQUEST="true",
        )

        prescription.refresh_from_db()
        assert prescription.cancel_date == date(2026, 10, 7)


class TestEquipmentRights:
    EQ = {
        "eq-name": "Взлом",
        "eq-card_label": "",
        "eq-synonyms": [""],
        "eq-window_start": "13:00",
        "eq-window_end": "15:00",
        "eq-step_min": "15",
        "eq-duration_min": "15",
        "eq-capacity": "3",
    }

    def test_doctor_cannot_add_or_edit_equipment(self, client, doctor, procedures):
        st = Equipment.objects.get(name="st-150")
        client.force_login(doctor)

        assert client.post(reverse("scheduling:equipment_add"), self.EQ).status_code == 403
        response = client.post(reverse("scheduling:equipment_edit", args=[st.pk]), self.EQ)

        assert response.status_code == 403
        st.refresh_from_db()
        assert (st.name, st.capacity) == ("st-150", 1)
        assert not Equipment.objects.filter(name="Взлом").exists()

    def test_catalog_right_alone_archives_equipment_procedure(
        self, client, make_user, department, doctor, make_program, procedures
    ):
        """Архив тренажёра из «Справочников» — право «Справочники», без «Групповых занятий»."""
        item = procedures["equipment"]
        add_prescription(doctor, Prescription(program=make_program(), procedure=item))
        keeper = make_user("keeper", (department, Preset.DOCTOR), rights=[Right.CATALOG])
        client.force_login(keeper)

        htmx(client, reverse("catalog:procedure_delete", args=[item.pk]), {"confirm": "1"})

        item.refresh_from_db()
        assert not item.is_active and not item.equipment.is_active
        assert (
            client.post(
                reverse("scheduling:equipment_restore", args=[item.equipment_id])
            ).status_code
            == 403
        )


class TestHoldersPrivacy:
    def test_other_department_patients_not_named(
        self, rehab, make_user, other_department, procedures
    ):
        from apps.programs.models import Program
        from apps.programs.services import save_program

        stranger = make_user("stranger", (other_department, Preset.DOCTOR), short_name="Д")
        program = save_program(
            stranger,
            Program(
                department=other_department,
                full_name="Чужев Тест Тестович",
                history_number="1",
                room="1",
                shrm=3,
                attending_doctor=stranger,
                start_date=TUE,
            ),
            end_date_changed=False,
        )
        add_prescription(
            stranger, Prescription(program=program, procedure=procedures["individual"])
        )

        with pytest.raises(services.CatalogError) as error:
            services.delete_procedure(rehab, procedures["individual"])

        assert "(1)" in str(error.value)  # число — можно, фамилия чужого пациента — нет
        assert "Чужев" not in str(error.value)
        assert "1 в других отделениях" in str(error.value)
