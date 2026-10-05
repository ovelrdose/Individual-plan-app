from datetime import date

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.programs.models import Prescription, Program


def form_data(doctor, **fields):
    return {
        "full_name": "Тестова Анна Сергеевна",
        "sex": "Ж",
        "age": "",
        "history_number": "",
        "room": "5а",
        "shrm": "3",
        "diagnosis": "",
        "attending_doctor": doctor.pk,
        "start_date": "2026-10-05",
        "end_date": "",
    } | fields


class TestList:
    def test_home_redirects_to_programs(self, client, doctor):
        client.force_login(doctor)

        assert client.get("/").url == reverse("programs:list")

    def test_buttons_and_badge(self, client, doctor, make_program):
        program = make_program(start_date=date.today())
        Prescription.objects.create(program=program, raw_text="непонятное")
        client.force_login(doctor)

        html = client.get(reverse("programs:list")).content.decode()

        assert "Загрузить лист назначений" in html
        assert "Создать вручную" in html
        assert "требует проверки" in html

    def test_rehab_sees_list_without_buttons(self, client, rehab, make_program):
        make_program(start_date=date.today())
        client.force_login(rehab)

        html = client.get(reverse("programs:list")).content.decode()

        assert "Тестов Тест Тестович" in html
        assert "Загрузить лист назначений" not in html

    def test_bad_period_is_ignored(self, client, doctor):
        client.force_login(doctor)

        assert client.get(reverse("programs:list"), {"period": "xxx"}).status_code == 200


class TestProgramScreens:
    def test_create_manually_with_minimal_fields(self, client, doctor):
        client.force_login(doctor)

        page = client.get(reverse("programs:create"))
        response = client.post(reverse("programs:create"), form_data(doctor))

        assert page.context["form"]["start_date"].value() == date.today()
        program = Program.objects.get()
        assert response.url == reverse("programs:detail", args=[program.pk])
        assert (program.end_date, program.source, program.age) == (
            date(2026, 10, 15),
            "manual",
            None,
        )

    def test_shift_start_past_old_end_recomputes(self, client, doctor, make_program):
        program = make_program(shrm=4)
        client.force_login(doctor)

        client.post(
            reverse("programs:edit", args=[program.pk]),
            form_data(
                doctor, shrm="4", start_date="2026-10-25", end_date=program.end_date.isoformat()
            ),
        )
        program.refresh_from_db()

        assert program.end_date == date(2026, 11, 8)

    def test_manual_end_date(self, client, doctor, make_program):
        program = make_program(shrm=4)
        client.force_login(doctor)

        client.post(
            reverse("programs:edit", args=[program.pk]),
            form_data(doctor, shrm="4", end_date="2026-10-22"),
        )
        program.refresh_from_db()

        assert (program.end_date, program.end_date_manual) == (date(2026, 10, 22), True)

    def test_course_conflict_is_shown(self, client, doctor, make_program, procedures):
        program = make_program(shrm=4)
        Prescription.objects.create(
            program=program, procedure=procedures["group"], start_date=date(2026, 10, 15)
        )
        client.force_login(doctor)

        response = client.post(
            reverse("programs:edit", args=[program.pk]),
            form_data(doctor, shrm="4", end_date="2026-10-10"),
        )

        assert "Сначала поправьте или удалите" in response.content.decode()

    def test_detail_shows_review_and_dismiss(self, client, doctor, make_program):
        program = make_program()
        program.import_warnings = ["В листе разные значения «ИБ»"]
        program.save()
        client.force_login(doctor)

        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "Проверьте после импорта" in html
        assert "Скачать карту" in html

        client.post(reverse("programs:dismiss_warnings", args=[program.pk]))
        html = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "Проверьте после импорта" not in html

    def test_rehab_view_only(self, client, rehab, make_program):
        program = make_program()
        client.force_login(rehab)

        detail = client.get(reverse("programs:detail", args=[program.pk]))
        assert detail.status_code == 200
        assert detail.context["add_form"] is None
        assert client.get(reverse("programs:create")).status_code == 403
        assert client.get(reverse("programs:edit", args=[program.pk])).status_code == 403
        assert (
            client.post(reverse("programs:dismiss_warnings", args=[program.pk])).status_code == 403
        )

    def test_foreign_department_is_404(self, client, make_user, other_department, make_program):
        program = make_program()
        client.force_login(make_user("stranger", (other_department, Role.DOCTOR)))

        for name in ("programs:detail", "programs:edit"):
            assert client.get(reverse(name, args=[program.pk])).status_code == 404


class TestPrescriptionScreens:
    def test_add_and_errors(self, client, doctor, make_program, procedures):
        program = make_program()
        client.force_login(doctor)
        url = reverse("programs:prescription_add", args=[program.pk])

        ok = client.post(
            url, {"procedure": procedures["group"].pk, "per_day": "1", "in_card": "on"}
        )
        empty = client.post(url, {"per_day": "1"})
        over = client.post(url, {"procedure": procedures["individual"].pk, "per_day": "3"})

        assert '<div id="prescriptions">' in ok.content.decode()
        assert "Выберите процедуру из списка." in empty.content.decode()
        assert over.context["add_form"].procedure_label == "Индивидуальное занятие"
        assert program.prescriptions.count() == 1

    def test_unrecognized_row_flow(self, client, doctor, make_program, procedures):
        program = make_program()
        item = Prescription.objects.create(program=program, raw_text="Thera Trainer 15 мин")
        client.force_login(doctor)
        url = reverse("programs:prescription_edit", args=[program.pk, item.pk])

        detail = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        form_page = client.get(url)
        saved = client.post(
            url, {"edit-procedure": procedures["card_only"].pk, "edit-per_day": "1"}
        )

        assert "Выбрать процедуру" in detail
        assert "Из листа: Thera Trainer 15 мин" in form_page.content.decode()
        assert saved.context["editing"] is None
        assert Prescription.objects.get(pk=item.pk).procedure == procedures["card_only"]

    def test_edit_requires_procedure(self, client, doctor, make_program):
        program = make_program()
        item = Prescription.objects.create(program=program, raw_text="непонятное")
        client.force_login(doctor)

        response = client.post(
            reverse("programs:prescription_edit", args=[program.pk, item.pk]), {"edit-per_day": "1"}
        )

        assert "procedure" in response.context["edit_form"].errors

    def test_move_delete_and_isolation(self, client, doctor, make_program, procedures):
        one = make_program()
        other = make_program(full_name="Другой Д.Д.", history_number="2")
        first = Prescription.objects.create(
            program=one, procedure=procedures["group"], card_order=1
        )
        second = Prescription.objects.create(
            program=one, procedure=procedures["card_only"], card_order=2
        )
        foreign = Prescription.objects.create(program=other, procedure=procedures["group"])
        client.force_login(doctor)

        client.post(reverse("programs:prescription_move", args=[one.pk, second.pk, "up"]))
        assert [p.pk for p in one.prescriptions.all()] == [second.pk, first.pk]
        assert (
            client.post(
                reverse("programs:prescription_move", args=[one.pk, second.pk, "left"])
            ).status_code
            == 404
        )
        assert (
            client.post(
                reverse("programs:prescription_delete", args=[one.pk, foreign.pk])
            ).status_code
            == 404
        )
        client.post(reverse("programs:prescription_delete", args=[one.pk, second.pk]))
        assert list(one.prescriptions.all()) == [first]

    def test_rehab_gets_403(self, client, rehab, make_program, procedures):
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=procedures["group"])
        client.force_login(rehab)

        assert (
            client.get(
                reverse("programs:prescription_edit", args=[program.pk, item.pk])
            ).status_code
            == 403
        )
        assert (
            client.post(
                reverse("programs:prescription_delete", args=[program.pk, item.pk])
            ).status_code
            == 403
        )

    def test_search_uses_program_department(
        self, client, make_user, department, other_department, procedures
    ):
        procedures["card_only"].department = other_department
        procedures["card_only"].save()
        user = make_user("both", (department, Role.DOCTOR), (other_department, Role.DOCTOR))
        client.force_login(user)
        url = reverse("catalog:procedure_search")

        own = client.get(url, {"q": "эрготерапевт", "department": other_department.pk})
        main = client.get(url, {"q": "эрготерапевт", "department": department.pk})

        assert [p.name for p in own.context["procedures"]] == ["Эрготерапевт"]
        assert main.context["procedures"] == []

    @pytest.mark.parametrize(("query", "expected"), [("st150", ["st-150"]), ("йога", [])])
    def test_search(self, client, doctor, procedures, query, expected):
        client.force_login(doctor)

        response = client.get(reverse("catalog:procedure_search"), {"q": query})

        assert [p.name for p in response.context["procedures"]] == expected
