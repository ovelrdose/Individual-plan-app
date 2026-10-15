from datetime import date, time

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.cards.models import CardExport
from apps.catalog.models import GroupSession
from apps.programs import services as programs_services
from apps.programs.models import Prescription, Program, WithdrawalReason
from apps.scheduling import board_days
from apps.scheduling.models import Booking


def form_data(doctor, **fields):
    return {
        "full_name": "Тестова Анна Сергеевна",
        "sex": "Ж",
        "age": "",
        "history_number": "",
        "room": "5а",
        "shrm": "3",
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
        assert detail.context["can_edit"] is False
        page = detail.content.decode()
        assert reverse("programs:prescription_add", args=[program.pk]) not in page
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

        assert "Новое назначение" in client.get(url).content.decode()
        ok = client.post(
            url, {"procedure": procedures["group"].pk, "per_day": "1", "in_card": "on"}
        )
        empty = client.post(url, {"per_day": "1"})
        over = client.post(url, {"procedure": procedures["individual"].pk, "per_day": "3"})

        # Добавили — панель закрывается, таблица и расписание приходят вне цели (hx-swap-oob).
        assert '<div id="prescription-panel" class="d-none">' in ok.content.decode()
        assert '<div id="prescriptions" hx-swap-oob="true">' in ok.content.decode()
        assert "Выберите процедуру из списка." in empty.content.decode()
        assert over.context["form"].procedure_label == "Индивидуальное занятие"
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
        assert "Из листа:</span> Thera Trainer 15 мин" in form_page.content.decode()
        assert saved.context["editing"] is None
        assert Prescription.objects.get(pk=item.pk).procedure == procedures["card_only"]

    def test_edit_requires_procedure(self, client, doctor, make_program):
        program = make_program()
        item = Prescription.objects.create(program=program, raw_text="непонятное")
        client.force_login(doctor)

        response = client.post(
            reverse("programs:prescription_edit", args=[program.pk, item.pk]), {"edit-per_day": "1"}
        )

        assert "procedure" in response.context["form"].errors

    def test_group_time(self, client, doctor, make_program, procedures):
        """Время группы из листа правится в назначении; подбор ставит занятие в это время,
        времени без занятия группы быть не может (TZ.md, решение 71)."""
        group = procedures["group"]
        for start in (time(9, 10), time(13, 40)):
            GroupSession.objects.create(procedure=group, start_time=start, duration_min=30)
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=group)
        client.force_login(doctor)
        url = reverse("programs:prescription_edit", args=[program.pk, item.pk])
        data = {"edit-procedure": group.pk, "edit-per_day": "1", "edit-in_card": "on"}

        wrong = client.post(url, data | {"edit-preferred_time": "11:00"})
        client.post(url, data | {"edit-preferred_time": "13:40"})

        assert "нет занятия в это время" in wrong.content.decode()
        item.refresh_from_db()
        assert item.preferred_time == time(13, 40)
        assert set(item.bookings.values_list("start", flat=True)) == {time(13, 40)}
        page = client.get(reverse("programs:detail", args=[program.pk])).content.decode()
        assert "в 13:40" in page

    def test_group_time_choices(self, client, doctor, make_program, procedures):
        """Время группы выбирается из её занятий, а не вводится вручную."""
        group = procedures["group"]
        for start in (time(9, 10), time(13, 40)):
            GroupSession.objects.create(procedure=group, start_time=start, duration_min=30)
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=group)
        card = Prescription.objects.create(program=program, procedure=procedures["card_only"])
        client.force_login(doctor)

        group_panel = client.get(reverse("programs:prescription_edit", args=[program.pk, item.pk]))
        card_panel = client.get(reverse("programs:prescription_edit", args=[program.pk, card.pk]))

        assert group_panel.context["form"].session_times == [time(9, 10), time(13, 40)]
        assert '<option value="13:40:00">13:40</option>' in group_panel.content.decode()
        assert "Время группы" not in card_panel.content.decode()

    def test_table_and_panel(self, client, doctor, make_program, procedures, monkeypatch):
        """Действующие — в таблице, отменённые — свёрнуты внизу; открытая строка выделена."""
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 8))
        program = make_program()
        active = Prescription.objects.create(
            program=program, procedure=procedures["card_only"], card_order=1
        )
        Prescription.objects.create(
            program=program,
            procedure=procedures["group"],
            cancel_date=date(2026, 10, 8),
            card_order=2,
        )
        client.force_login(doctor)

        detail = client.get(reverse("programs:detail", args=[program.pk]))
        panel = client.get(reverse("programs:prescription_edit", args=[program.pk, active.pk]))

        assert [p.pk for p in detail.context["active"]] == [active.pk]
        assert [p.label for p in detail.context["cancelled"]] == ["Эрго общая"]
        assert "Отменённые (1)" in detail.content.decode()
        assert panel.context["selected"] == active.pk
        assert "table-active" in panel.content.decode()
        assert "Отменить с завтра" in panel.content.decode()

    def test_cancel_from_tomorrow_and_restore(
        self, client, doctor, make_program, procedures, monkeypatch
    ):
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 8))
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=procedures["card_only"])
        client.force_login(doctor)
        url = reverse("programs:prescription_cancel", args=[program.pk, item.pk])

        cancelled = client.post(url)
        item.refresh_from_db()
        assert item.cancel_date == date(2026, 10, 9)
        assert "Снять отмену" in cancelled.content.decode()

        client.post(url, {"restore": "1"})
        item.refresh_from_db()
        assert item.cancel_date is None

    def test_cancel_error_is_a_message(self, client, doctor, make_program, procedures, monkeypatch):
        """Строка начинается позже завтрашнего дня — отменять с завтра нельзя."""
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 8))
        program = make_program()
        item = Prescription.objects.create(
            program=program, procedure=procedures["card_only"], start_date=date(2026, 10, 15)
        )
        client.force_login(doctor)

        response = client.post(reverse("programs:prescription_cancel", args=[program.pk, item.pk]))

        assert "Отменить можно не раньше следующего дня после начала." in response.content.decode()
        item.refresh_from_db()
        assert item.cancel_date is None

    def test_reorder_keeps_cancelled_in_place(
        self, client, doctor, make_program, procedures, monkeypatch
    ):
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 8))
        program = make_program()
        a, b, c = (
            Prescription.objects.create(program=program, procedure=procedures[kind], card_order=n)
            for n, kind in enumerate(("group", "card_only", "equipment"), start=1)
        )
        b.cancel_date = date(2026, 10, 7)
        b.save()
        client.force_login(doctor)
        url = reverse("programs:prescription_reorder", args=[program.pk])

        client.post(url, {"order": [c.pk, a.pk]})
        stale = client.post(url, {"order": [a.pk]})

        assert [p.pk for p in program.prescriptions.all()] == [c.pk, b.pk, a.pk]
        assert "Список назначений изменился" in stale.content.decode()
        assert client.post(url, {"order": ["x"]}).status_code == 404

    def test_move_skips_cancelled(self, client, doctor, make_program, procedures, monkeypatch):
        """Стрелка «выше» перескакивает свёрнутую отменённую строку."""
        monkeypatch.setattr(board_days, "today", lambda: date(2026, 10, 8))
        program = make_program()
        a, b, c = (
            Prescription.objects.create(program=program, procedure=procedures[kind], card_order=n)
            for n, kind in enumerate(("group", "card_only", "equipment"), start=1)
        )
        b.cancel_date = date(2026, 10, 7)
        b.save()
        client.force_login(doctor)

        response = client.post(reverse("programs:prescription_move", args=[program.pk, c.pk, "up"]))

        assert [p.pk for p in program.prescriptions.all()] == [c.pk, b.pk, a.pk]
        assert response.context["editing"].pk == c.pk, "панель остаётся открытой"

    def test_withdrawn_patient_gets_message(self, client, doctor, make_program, procedures):
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=procedures["card_only"])
        programs_services.withdraw(doctor, program, date(2026, 10, 6), WithdrawalReason.REFUSAL)
        client.force_login(doctor)

        response = client.get(reverse("programs:prescription_edit", args=[program.pk, item.pk]))

        assert response.status_code == 200
        assert "Пациент выбыл" in response.content.decode()
        assert '<div id="prescription-panel" class="d-none">' in response.content.decode()

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


class TestDepartmentVisibility:
    """Все сотрудники отделения видят все его программы (TZ.md, решение 37)."""

    def test_rehab_sees_program_created_by_doctor(self, client, rehab, make_program):
        program = make_program(start_date=date.today())
        client.force_login(rehab)

        assert program.full_name in client.get(reverse("programs:list")).content.decode()
        detail = client.get(reverse("programs:detail", args=[program.pk]))
        assert detail.status_code == 200
        assert program.department.name in detail.content.decode()
        assert client.get(reverse("cards:download", args=[program.pk])).status_code == 200


class TestDeleteProgram:
    """Удаляет только администратор (TZ.md, решение 39)."""

    def test_only_admin(self, client, doctor, rehab, make_program):
        program = make_program()
        url = reverse("programs:delete", args=[program.pk])

        for user in (doctor, rehab):
            client.force_login(user)
            assert client.get(url).status_code == 403
            assert client.post(url).status_code == 403
            assert (
                "Удалить"
                not in client.get(reverse("programs:detail", args=[program.pk])).content.decode()
            )
        assert Program.objects.filter(pk=program.pk).exists()

    def test_admin_deletes_with_everything(
        self, client, admin_user, doctor, make_program, procedures
    ):
        program = make_program()
        item = Prescription.objects.create(program=program, procedure=procedures["group"])
        Booking.objects.create(
            program=program,
            prescription=item,
            procedure=item.procedure,
            kind="LFK_GROUP",
            date=program.start_date,
            start="09:10",
            end="09:40",
        )
        CardExport.objects.create(program=program, user=doctor)
        client.force_login(admin_user)
        url = reverse("programs:delete", args=[program.pk])

        confirm = client.get(url)
        response = client.post(url, follow=True)

        assert "Удалить программу?" in confirm.content.decode()
        assert response.redirect_chain[-1][0] == reverse("programs:list")
        assert "удалена" in response.content.decode()
        assert not Program.objects.exists()
        assert not Prescription.objects.exists()
        assert not Booking.objects.exists()
        assert not CardExport.objects.exists()
        assert Program.history.filter(history_type="-").get().history_user == admin_user
