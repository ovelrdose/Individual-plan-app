import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.accounts.access import SESSION_DEPARTMENT_KEY
from apps.accounts.models import Membership, Preset, Right, User
from apps.org.models import Department
from tests.conftest import PASSWORD


@pytest.fixture
def two_department_user(make_user, department, other_department):
    return make_user("both", (department, Preset.DOCTOR), (other_department, Preset.REHAB))


class TestHome:
    def test_shows_department(self, client, doctor):
        client.force_login(doctor)

        html = client.get(reverse("home"), follow=True).content.decode()

        assert "ОМР № 4" in html
        assert "Иванова А.А." in html
        assert "Пользователи" not in html, "без права — без пункта меню"
        assert 'name="department"' not in html, "одно отделение — без переключателя"

    def test_without_departments(self, client, make_user):
        client.force_login(make_user("nobody"))

        html = client.get(reverse("home"), follow=True).content.decode()

        assert "Нет доступа ни к одному отделению" in html

    def test_switcher_for_several_departments(self, client, two_department_user):
        client.force_login(two_department_user)

        html = client.get(reverse("home"), follow=True).content.decode()

        assert 'name="department"' in html
        assert "ОМР № 1" in html

    def test_admin_badge(self, client, admin_user, department):
        client.force_login(admin_user)

        html = client.get(reverse("home"), follow=True).content.decode()

        assert "Администратор" in html
        assert reverse("admin:index") in html


class TestSwitchDepartment:
    def test_switch_to_own(self, client, two_department_user, other_department):
        client.force_login(two_department_user)

        response = client.post(
            reverse("switch_department"), {"department": other_department.pk, "next": "/"}
        )

        assert response.status_code == 302
        assert client.session[SESSION_DEPARTMENT_KEY] == other_department.pk
        assert "ОМР № 1" in client.get(reverse("home"), follow=True).content.decode()

    def test_foreign_is_404(self, client, doctor, other_department):
        client.force_login(doctor)

        response = client.post(reverse("switch_department"), {"department": other_department.pk})

        assert response.status_code == 404

    def test_external_next_is_ignored(self, client, two_department_user, department):
        client.force_login(two_department_user)

        response = client.post(
            reverse("switch_department"),
            {"department": department.pk, "next": "https://example.com/"},
        )

        assert response.url == reverse("home")

    def test_get_not_allowed(self, client, doctor):
        client.force_login(doctor)

        assert client.get(reverse("switch_department")).status_code == 405


def test_password_change(client, doctor):
    client.force_login(doctor)
    new_password = "Новый-пароль-2026"

    response = client.post(
        reverse("password_change"),
        {
            "old_password": PASSWORD,
            "new_password1": new_password,
            "new_password2": new_password,
        },
    )

    assert response.status_code == 302
    doctor.refresh_from_db()
    assert doctor.check_password(new_password)


def test_password_change_rejects_short(client, doctor):
    client.force_login(doctor)

    response = client.post(
        reverse("password_change"),
        {"old_password": PASSWORD, "new_password1": "a1b2", "new_password2": "a1b2"},
    )

    assert response.status_code == 200
    doctor.refresh_from_db()
    assert doctor.check_password(PASSWORD)


def test_admin_requires_staff(client, doctor, admin_user):
    client.force_login(doctor)
    assert client.get(reverse("admin:index")).status_code == 302

    client.force_login(admin_user)
    assert client.get(reverse("admin:accounts_user_changelist")).status_code == 200
    assert client.get(reverse("admin:org_department_changelist")).status_code == 200


class TestSeedDev:
    def test_refuses_without_debug(self, db, settings):
        settings.DEBUG = False

        with pytest.raises(CommandError):
            call_command("seed_dev")

    def test_creates_and_is_idempotent(self, db, settings):
        settings.DEBUG = True

        call_command("seed_dev", password="x-pass-123")
        call_command("seed_dev", password="x-pass-123")

        assert Department.objects.filter(code="omr4").count() == 1
        assert User.objects.get(username="admin").is_superuser
        doctor, rehab = User.objects.get(username="doctor"), User.objects.get(username="rehab")
        assert Membership.objects.filter(user__in=[doctor, rehab]).count() == 2
        assert doctor.is_attending_doctor and Right.SCHEDULE not in doctor.rights
        assert not rehab.is_staff and Right.SCHEDULE in rehab.rights
        assert User.objects.get(username="doctor").check_password("x-pass-123")
