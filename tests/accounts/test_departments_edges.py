"""Отделения, снятие блокировки входа, админка только суперпользователю — граничные случаи и
права (TZ.md FR-ORG-1…3, FR-ACC-6, NFR-6, решение 83). Данные — вымышленные."""

from datetime import date, timedelta

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from apps.accounts import lockout
from apps.accounts.access import departments_for
from apps.accounts.models import Membership, Preset, Right
from apps.catalog.models import Procedure, ProcedureKind
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import import_prescription_sheet
from apps.org import services
from apps.org.models import Department
from apps.programs.domain import SHRM_UK
from apps.programs.models import Program, WithdrawalReason
from apps.programs.services import save_program, withdraw
from apps.scheduling import board_days
from tests.conftest import PASSWORD
from tests.sheets import make_sheet

TUE = date(2026, 10, 6)


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


def form_data(name: str = "ОМР № 7", code: str = "omr7", **changes) -> dict:
    data = {
        "dep-name": name,
        "dep-code": code,
        "dep-course_3": "12",
        "dep-course_4": "16",
        "dep-course_5": "21",
        "dep-course_uk": "14",
        "dep-max_individual_per_day": "1",
    }
    return data | changes


def new_program(user, department, shrm: int, **fields) -> Program:
    values = {
        "department": department,
        "full_name": "Отделенцев Тест Тестович",
        "sex": "М",
        "history_number": "700",
        "room": "1",
        "shrm": shrm,
        "attending_doctor": user,
        "start_date": TUE,
    } | fields
    return save_program(user, Program(**values), end_date_changed=False)


class TestCourseLength:
    @pytest.mark.parametrize(("shrm", "days"), [(3, 12), (4, 16), (5, 21), (SHRM_UK, 14)])
    def test_new_department_course_used_by_program(self, client, boss, make_user, shrm, days):
        client.force_login(boss)
        htmx(client, reverse("users:department_add"), form_data())
        new = Department.objects.get(code="omr7")
        assert new.max_individual_per_day == 1
        doctor = make_user("doc7", (new, Preset.DOCTOR), short_name="Седьмов С.С.")

        program = new_program(doctor, new, shrm)

        # Календарные даты курса считая день поступления (FR-ORG-1).
        assert program.end_date == TUE + timedelta(days=days - 1)
        assert len(program.course_dates()) == days

    def test_import_into_new_department_uses_its_course(self, client, boss, make_user):
        client.force_login(boss)
        htmx(client, reverse("users:department_add"), form_data())
        new = Department.objects.get(code="omr7")
        doctor = make_user("doc7", (new, Preset.DOCTOR), short_name="Седьмов С.С.")

        program = import_prescription_sheet(
            doctor,
            new,
            parse_sheet(make_sheet(shrm=3, rows=[("06.10.26", "Непонятное 10 мин", "")])),
            attending_doctor=doctor,
            force=True,
        )

        assert (program.start_date, program.end_date) == (TUE, TUE + timedelta(days=11))

    def test_edit_course_changes_only_new_programs(self, client, boss, department, make_program):
        old = make_program(shrm=3, start_date=TUE)
        client.force_login(boss)

        htmx(
            client,
            reverse("users:department_edit", args=[department.pk]),
            form_data("ОМР № 4", "omr4", **{"dep-course_3": "13"}),
        )

        department.refresh_from_db()
        assert department.course_length(3) == 13
        old.refresh_from_db()
        assert old.end_date == TUE + timedelta(days=10)  # уже заведённая не пересчитывается
        assert make_program(shrm=3, history_number="2", start_date=TUE).end_date == TUE + (
            timedelta(days=12)
        )


class TestInput:
    @pytest.mark.parametrize(
        ("changes", "text"),
        [
            ({"dep-code": "omr4"}, "уже существует"),
            ({"dep-name": "ОМР № 4"}, "уже существует"),
            ({"dep-code": "омр 7"}, "Значение должно состоять"),
            ({"dep-name": "   "}, "Обязательное поле"),
            ({"dep-course_4": ""}, "Обязательное поле"),
            ({"dep-course_uk": "-1"}, "больше либо равно 1"),
            ({"dep-course_5": "двадцать"}, "Введите целое число"),
            ({"dep-max_individual_per_day": "0"}, "больше либо равно 1"),
        ],
    )
    def test_bad_values_shown_in_window(self, client, boss, department, changes, text):
        client.force_login(boss)

        html = htmx(client, reverse("users:department_add"), form_data(**changes))

        assert text in html
        assert not Department.objects.exclude(pk=department.pk).exists()


class TestArchive:
    def test_refused_with_current_programs_in_window(self, client, boss, department, make_program):
        make_program(start_date=TUE)
        make_program(start_date=TUE, history_number="2", full_name="Второв Т.Т.")
        client.force_login(boss)
        url = reverse("users:department_delete", args=[department.pk])

        assert "Текущих программ: 2" in htmx(client, url)
        html = htmx(client, url, {"confirm": "1"})

        assert "текущих программ: 2" in html
        department.refresh_from_db()
        assert department.is_active

    def test_last_day_of_course_is_current(self, boss, department, make_program):
        make_program(shrm=3, start_date=TUE - timedelta(days=10))  # заканчивается сегодня
        with pytest.raises(services.DepartmentError, match="текущих программ: 1"):
            services.delete_department(boss, department)

    def test_withdrawn_patient_not_current(self, boss, rehab, department, make_program):
        program = make_program(start_date=date(2026, 10, 1))
        withdraw(rehab, program, date(2026, 10, 3), WithdrawalReason.EARLY_DISCHARGE)

        assert services.delete_department(boss, department).archived

    def test_only_members_archived_not_deleted(self, boss, department, other_department):
        assert services.delete_department(boss, other_department).archived is False
        assert not Department.objects.filter(pk=other_department.pk).exists()
        # department используется: в нём работает boss.
        assert services.delete_department(boss, department).archived

    def test_procedure_makes_department_used(self, boss, other_department):
        Procedure.objects.create(
            name="Своя", card_label="Своя", kind=ProcedureKind.CARD_ONLY,
            department=other_department,
        )  # fmt: skip
        assert services.delete_department(boss, other_department).archived

    def test_archived_hidden_from_members_and_import(
        self, client, boss, doctor, department, other_department
    ):
        Membership.objects.create(user=doctor, department=other_department)
        services.delete_department(boss, department)

        assert list(departments_for(doctor)) == [other_department]
        from django.core.exceptions import PermissionDenied

        department.refresh_from_db()
        with pytest.raises(PermissionDenied):
            import_prescription_sheet(
                doctor, department, parse_sheet(make_sheet()), attending_doctor=doctor
            )
        client.force_login(boss)
        html = client.get(reverse("users:departments")).content.decode()
        assert "В архиве (1)" in html

    def test_restore_via_window(self, client, boss, department, make_program):
        make_program(start_date=date(2026, 9, 1))
        services.delete_department(boss, department)
        client.force_login(boss)

        html = htmx(client, reverse("users:department_restore", args=[department.pk]))

        department.refresh_from_db()
        assert department.is_active and "снова действует" in html
        assert department.history.first().history_user == boss

    def test_already_archived(self, client, boss, department, make_program):
        make_program(start_date=date(2026, 9, 1))
        services.delete_department(boss, department)
        client.force_login(boss)
        url = reverse("users:department_delete", args=[department.pk])
        assert "уже в архиве" in htmx(client, url, {"confirm": "1"})


class TestDepartmentRights:
    @pytest.mark.parametrize(
        ("name", "with_pk"),
        [
            ("department_add", False),
            ("department_edit", True),
            ("department_delete", True),
            ("department_restore", True),
        ],
    )
    def test_rehab_post_forbidden_nothing_changes(
        self, client, rehab, other_department, name, with_pk
    ):
        if name == "department_restore":
            Department.objects.filter(pk=other_department.pk).update(is_active=False)
        before = list(Department.objects.order_by("pk").values_list("name", "is_active"))
        client.force_login(rehab)
        args = [other_department.pk] if with_pk else []

        response = client.post(
            reverse(f"users:{name}", args=args), form_data("Взлом") | {"confirm": 1}
        )

        assert response.status_code == 403
        assert list(Department.objects.order_by("pk").values_list("name", "is_active")) == before

    def test_anonymous_redirected(self, client, department):
        response = client.post(
            reverse("users:department_delete", args=[department.pk]), {"confirm": "1"}
        )
        assert response.status_code == 302 and "/login/" in response["Location"]
        assert Department.objects.filter(pk=department.pk).exists()


def login(client, username: str, password: str = PASSWORD):
    return client.post(reverse("login"), {"username": username, "password": password})


class TestUnlock:
    def test_locked_login_passes_after_unlock(self, client, boss, doctor, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            login(client, "doctor", "wrong-password")
        login(client, "doctor")
        assert "_auth_user_id" not in client.session  # верный пароль, но вход заблокирован
        until = lockout.locked_until("doctor")
        assert until is not None

        client.force_login(boss)
        html = client.get(reverse("users:list")).content.decode()
        local = timezone.localtime(until).strftime("%H:%M")
        assert f"вход заблокирован до {local}" in html
        client.post(reverse("users:unlock", args=[doctor.pk]))
        client.logout()

        response = login(client, "doctor")

        assert response.status_code == 302 and "_auth_user_id" in client.session

    def test_nine_failures_not_locked(self, client, doctor, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS - 1):
            login(client, "doctor", "wrong-password")
        assert lockout.locked_until("doctor") is None
        login(client, "doctor")
        assert "_auth_user_id" in client.session

    def test_unlock_mixed_case_username(self, client, boss, make_user, department, settings):
        user = make_user("Smirnova", (department, Preset.DOCTOR))
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            login(client, "SMIRNOVA", "wrong-password")
        assert lockout.is_locked(user.username)
        client.force_login(boss)

        client.post(reverse("users:unlock", args=[user.pk]))

        assert not lockout.is_locked("smirnova")

    def test_admin_preset_cannot_unlock_superuser(self, client, boss, admin_user, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            lockout.register_failure("admin")
        client.force_login(boss)

        assert client.post(reverse("users:unlock", args=[admin_user.pk])).status_code == 403
        assert lockout.is_locked("admin")

    def test_unlock_get_not_allowed(self, client, boss, doctor, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            lockout.register_failure("doctor")
        client.force_login(boss)
        assert client.get(reverse("users:unlock", args=[doctor.pk])).status_code == 405
        assert lockout.is_locked("doctor")

    def test_doctor_cannot_unlock_and_nothing_changes(self, client, doctor, rehab, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            lockout.register_failure("rehab")
        client.force_login(doctor)
        assert client.post(reverse("users:unlock", args=[rehab.pk])).status_code == 403
        assert lockout.is_locked("rehab")


class TestAdminSite:
    """Админка Django — только суперпользователю: ни один раздел не открывается остальным."""

    def changelists(self) -> list[str]:
        return [
            reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
            for model in admin.site._registry
        ]

    def test_all_sections_closed_for_everyone_but_superuser(
        self, client, make_user, department, rehab, doctor
    ):
        everything = make_user("all", (department, Preset.ADMIN), rights=list(Right))
        urls = [reverse("admin:index"), *self.changelists()]
        for user in (doctor, rehab, everything):
            client.force_login(user)
            for url in urls:
                response = client.get(url)
                assert response.status_code == 302, (user.username, url)
                assert "/admin/login/" in response["Location"]

    def test_all_sections_open_for_superuser(self, client, admin_user):
        client.force_login(admin_user)
        for url in [reverse("admin:index"), *self.changelists()]:
            assert client.get(url).status_code == 200, url

    def test_user_made_superuser_on_screen_gets_staff(self, admin_user, rehab):
        rehab.is_superuser = True
        rehab.save()
        rehab.refresh_from_db()
        assert rehab.is_staff
        rehab.is_superuser = False
        rehab.save()
        rehab.refresh_from_db()
        assert not rehab.is_staff

    def test_admin_post_by_non_superuser_changes_nothing(self, client, rehab, department):
        rehab.is_staff = True  # даже если флаг выставили в обход — сигнал снимает его
        rehab.save()
        client.force_login(rehab)
        response = client.post(
            reverse("admin:org_department_change", args=[department.pk]),
            {"name": "Взлом", "code": "hack"},
        )
        assert response.status_code == 302
        department.refresh_from_db()
        assert department.name == "ОМР № 4"

    def test_menu(self, client, make_user, department, doctor, admin_user):
        clerk = make_user("clerk", (department, Preset.DOCTOR), rights=[Right.USERS])
        client.force_login(clerk)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert reverse("catalog:index") in html and reverse("admin:index") not in html

        client.force_login(doctor)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert reverse("catalog:index") not in html and reverse("admin:index") not in html

        client.force_login(admin_user)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert reverse("catalog:index") in html and reverse("admin:index") in html


class TestIndividualLimit:
    def test_limit_from_window_applies_to_prescriptions(
        self, client, boss, department, doctor, make_program, procedures
    ):
        from django.core.exceptions import ValidationError

        from apps.programs.models import Prescription
        from apps.programs.services import add_prescription

        client.force_login(boss)
        htmx(
            client,
            reverse("users:department_edit", args=[department.pk]),
            form_data("ОМР № 4", "omr4", **{"dep-max_individual_per_day": "1"}),
        )
        department.refresh_from_db()
        program = make_program()

        with pytest.raises(ValidationError, match="не больше 1"):
            add_prescription(
                doctor,
                Prescription(program=program, procedure=procedures["individual"], per_day=2),
            )
        add_prescription(
            doctor, Prescription(program=program, procedure=procedures["individual"], per_day=1)
        )
