"""Отделения, снятие блокировки, админка — сценарии, которых нет в test_departments*.py и
test_admin_access.py (TZ.md FR-ORG-3, FR-ACC-6, NFR-6, решение 83). Данные — вымышленные."""

from datetime import date, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.accounts import lockout
from apps.accounts.models import LoginFailure, Preset
from apps.org import services
from apps.org.models import Department
from apps.scheduling import board_days

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
    return {
        "dep-name": name,
        "dep-code": code,
        "dep-course_3": "11",
        "dep-course_4": "15",
        "dep-course_5": "20",
        "dep-course_uk": "15",
        "dep-max_individual_per_day": "2",
    } | changes


class TestWindow:
    def test_edit_window_shows_each_course_field(self, client, boss, department):
        Department.objects.filter(pk=department.pk).update(
            course_days={"3": 11, "4": 16, "5": 21, "УК": 13}, max_individual_per_day=1
        )
        client.force_login(boss)

        html = client.get(reverse("users:department_edit", args=[department.pk])).content
        html = html.decode()

        for name, value in [
            ("course_3", 11),
            ("course_4", 16),
            ("course_5", 21),
            ("course_uk", 13),
            ("max_individual_per_day", 1),
        ]:
            assert f'name="dep-{name}" value="{value}"' in html

    def test_new_department_window_has_defaults(self, client, boss):
        client.force_login(boss)

        html = client.get(reverse("users:department_add")).content.decode()

        assert 'name="dep-course_3" value="11"' in html
        assert 'name="dep-course_5" value="20"' in html
        assert 'name="dep-course_uk" value="15"' in html

    def test_form_error_200_in_window_with_table_oob(self, client, boss, department):
        client.force_login(boss)

        html = htmx(client, reverse("users:department_add"), form_data(code=""))

        assert html.lstrip().startswith('<div id="department-panel"')
        assert "Обязательное поле" in html
        assert '<div id="departments-table" data-panel-table hx-swap-oob="true">' in html
        assert Department.objects.count() == 1


class TestArchiveGaps:
    def test_future_program_blocks_archive(self, boss, department, make_program):
        """Пациент поступит завтра — курс текущий, отделение в архив не уходит."""
        make_program(start_date=TUE + timedelta(days=1))

        with pytest.raises(services.DepartmentError, match="текущих программ: 1"):
            services.delete_department(boss, department)

    def test_unused_delete_logged_with_author(self, client, boss):
        empty = Department.objects.create(name="Пустое", code="empty")
        client.force_login(boss)

        html = htmx(client, reverse("users:department_delete", args=[empty.pk]), {"confirm": "1"})

        assert "Отделение «Пустое» удалено" in html
        last = Department.history.filter(id=empty.pk).order_by("-history_date").first()
        assert last.history_type == "-" and last.history_user == boss
        table = html[html.index('id="departments-table"') :]
        assert "Пустое" not in table

    def test_archive_logged_with_author(self, boss, department, make_program):
        make_program(start_date=date(2026, 9, 1))

        services.delete_department(boss, department)

        last = department.history.order_by("-history_date").first()
        assert (last.is_active, last.history_user) == (False, boss)

    def test_archived_window_offers_restore(self, client, boss, department, make_program):
        make_program(start_date=date(2026, 9, 1))
        services.delete_department(boss, department)
        client.force_login(boss)

        html = client.get(reverse("users:department_edit", args=[department.pk])).content
        html = html.decode()

        assert "Вернуть из архива" in html and "в архиве" in html
        assert reverse("users:department_delete", args=[department.pk]) not in html

    def test_programs_kept_after_archive(self, boss, department, make_program):
        program = make_program(start_date=date(2026, 9, 1))

        services.delete_department(boss, department)

        program.refresh_from_db()
        assert program.department_id == department.pk

    def test_missing_department_404(self, client, boss):
        client.force_login(boss)
        assert client.post(reverse("users:department_delete", args=[999999])).status_code == 404
        assert client.get(reverse("users:department_edit", args=[999999])).status_code == 404


class TestUnlockGaps:
    def lock(self, settings, username: str, minutes_ago: int) -> None:
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            lockout.register_failure(username)
        LoginFailure.objects.filter(username=username).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago)
        )

    def test_locked_until_shown_in_local_time(self, client, boss, doctor, settings):
        self.lock(settings, "doctor", minutes_ago=5)
        until = lockout.locked_until("doctor")
        client.force_login(boss)

        html = client.get(reverse("users:list")).content.decode()

        expected = timezone.localtime(until).strftime("%H:%M")
        assert f"вход заблокирован до {expected}" in html
        # 15 минут от самой ранней из последних неудач: осталось около 10 минут.
        left = until - timezone.now()
        assert timedelta(minutes=9) < left <= timedelta(minutes=10)

    def test_expired_failures_no_badge(self, client, boss, doctor, settings):
        self.lock(settings, "doctor", minutes_ago=settings.LOGIN_LOCKOUT_MINUTES + 1)
        client.force_login(boss)

        html = client.get(reverse("users:list")).content.decode()

        assert "вход заблокирован" not in html and "Снять блокировку" not in html

    def test_unlock_other_user_untouched(self, client, boss, doctor, rehab, settings):
        self.lock(settings, "doctor", minutes_ago=1)
        self.lock(settings, "rehab", minutes_ago=1)
        client.force_login(boss)

        client.post(reverse("users:unlock", args=[doctor.pk]))

        assert not lockout.is_locked("doctor") and lockout.is_locked("rehab")

    def test_unlock_message(self, client, boss, doctor, settings):
        self.lock(settings, "doctor", minutes_ago=1)
        client.force_login(boss)

        response = client.post(reverse("users:unlock", args=[doctor.pk]), follow=True)

        assert "Блокировка входа «doctor» снята" in response.content.decode()

    def test_unlock_missing_user_404(self, client, boss):
        client.force_login(boss)
        assert client.post(reverse("users:unlock", args=[999999])).status_code == 404

    def test_anonymous_cannot_unlock(self, client, doctor, settings):
        self.lock(settings, "doctor", minutes_ago=1)

        response = client.post(reverse("users:unlock", args=[doctor.pk]))

        assert response.status_code == 302 and "/login/" in response["Location"]
        assert lockout.is_locked("doctor")


class TestAdminSiteAccess:
    @pytest.mark.parametrize("preset", [Preset.ADMIN, Preset.REHAB, Preset.DOCTOR])
    def test_admin_login_refused_for_non_superuser(self, client, make_user, department, preset):
        """Через форму входа админки обычный пользователь не входит (is_staff снят)."""
        from tests.conftest import PASSWORD

        make_user("worker", (department, preset))

        response = client.post(reverse("admin:login"), {"username": "worker", "password": PASSWORD})

        assert response.status_code == 200  # форма с ошибкой, без перехода в админку
        assert client.get(reverse("admin:index")).status_code == 302

    def test_superuser_admin_login(self, client, admin_user):
        from tests.conftest import PASSWORD

        response = client.post(reverse("admin:login"), {"username": "admin", "password": PASSWORD})

        assert response.status_code == 302
        assert client.get(reverse("admin:index")).status_code == 200

    def test_superuser_loses_staff_when_demoted(self, admin_user):
        admin_user.is_superuser = False
        admin_user.save()
        admin_user.refresh_from_db()

        assert not admin_user.is_staff


@pytest.mark.django_db(transaction=True)
def test_migration_0004_drops_staff_on_real_schema():
    """accounts.0004 на настоящей схеме 0003: is_staff остаётся только у суперпользователя."""
    before = [("accounts", "0003_user_rights")]
    after = [("accounts", "0004_staff_only_superusers")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    User = executor.loader.project_state(before).apps.get_model("accounts", "User")
    User.objects.create(username="rehab-old", is_staff=True, rights=["catalog"])
    User.objects.create(username="admin-old", is_staff=True, is_superuser=True)
    User.objects.create(username="doctor-old", is_staff=False)

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    User = executor.loader.project_state(after).apps.get_model("accounts", "User")
    staff = dict(User.objects.values_list("username", "is_staff"))

    assert staff == {"rehab-old": False, "admin-old": True, "doctor-old": False}
    # Тестовая база остаётся на последней схеме для следующих тестов.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
