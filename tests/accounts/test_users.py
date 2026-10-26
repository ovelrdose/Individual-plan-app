"""Экран «Пользователи»: права-галочки, отделения, пароль (TZ.md §3, решение 79)."""

import pytest
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import Membership, Preset, Right, User
from tests.conftest import PASSWORD

NEW_PASSWORD = "Novyi-parol-2026"


@pytest.fixture
def boss(make_user, department):
    """Администратор центра — право «Пользователи и отделения», не суперпользователь."""
    return make_user("boss", (department, Preset.ADMIN), short_name="Главная Г.Г.")


def form_data(department, **fields) -> dict:
    return {
        "username": "petrova",
        "last_name": "Петрова",
        "first_name": "Анна",
        "short_name": "Петрова А.А.",
        "is_active": "on",
        "departments": [department.pk],
        "rights": [Right.PROGRAMS_EDIT, Right.SCHEDULE],
        "password1": NEW_PASSWORD,
        "password2": NEW_PASSWORD,
    } | fields


class TestAccess:
    @pytest.mark.parametrize("user_fixture", ["doctor", "rehab"])
    def test_without_users_right(self, client, request, user_fixture, doctor):
        client.force_login(request.getfixturevalue(user_fixture))

        assert client.get(reverse("users:list")).status_code == 403
        assert client.get(reverse("users:create")).status_code == 403
        assert client.get(reverse("users:edit", args=[doctor.pk])).status_code == 403
        assert client.post(reverse("users:password", args=[doctor.pk])).status_code == 403

    def test_list_and_menu(self, client, boss, doctor, rehab):
        client.force_login(boss)

        page = client.get(reverse("users:list"))
        menu = client.get(reverse("home"), follow=True).content.decode()

        html = page.content.decode()
        assert page.status_code == 200
        assert "doctor" in html and "rehab" in html and "удаление программ" in html
        assert reverse("users:list") in menu

    def test_superuser_is_edited_only_by_superuser(self, client, boss, admin_user):
        client.force_login(boss)

        assert client.get(reverse("users:edit", args=[admin_user.pk])).status_code == 403
        assert (
            reverse("users:edit", args=[admin_user.pk])
            not in client.get(reverse("users:list")).content.decode()
        )
        client.force_login(admin_user)
        assert client.get(reverse("users:edit", args=[boss.pk])).status_code == 200


class TestCreateAndEdit:
    def test_create(self, client, boss, department):
        client.force_login(boss)

        response = client.post(reverse("users:create"), form_data(department))

        assert response.status_code == 302, response.context["form"].errors
        user = User.objects.get(username="petrova")
        assert user.rights == [Right.PROGRAMS_EDIT, Right.SCHEDULE]
        assert list(user.departments.all()) == [department]
        assert user.check_password(NEW_PASSWORD)
        assert not user.is_staff and not user.is_attending_doctor

    def test_create_errors(self, client, boss, department):
        client.force_login(boss)

        mismatch = client.post(
            reverse("users:create"), form_data(department, password2="другой-пароль-1")
        )
        no_signature = client.post(
            reverse("users:create"),
            form_data(department, is_attending_doctor="on", short_name=""),
        )

        assert "password2" in mismatch.context["form"].errors
        assert "short_name" in no_signature.context["form"].errors
        assert not User.objects.filter(username="petrova").exists()

    def test_edit_rights_and_departments(self, client, boss, doctor, department, other_department):
        client.force_login(boss)
        data = form_data(
            department,
            username="doctor",
            short_name="Иванова А.А.",
            is_attending_doctor="on",
            departments=[other_department.pk],
            rights=[Right.PROGRAMS_EDIT, Right.PROGRAMS_DELETE, Right.CATALOG],
        )

        response = client.post(reverse("users:edit", args=[doctor.pk]), data)

        assert response.status_code == 302, response.context["form"].errors
        doctor.refresh_from_db()
        assert doctor.rights == [Right.PROGRAMS_EDIT, Right.PROGRAMS_DELETE, Right.CATALOG]
        assert list(doctor.departments.all()) == [other_department]
        assert not doctor.is_staff, "админка — только суперпользователю (решение 83)"
        assert doctor.check_password(PASSWORD), "пароль при правке не меняется"

    def test_presets_on_form(self, client, boss, department):
        client.force_login(boss)

        page = client.get(reverse("users:create"))
        html = page.content.decode()

        assert page.context["form"].fields["departments"].initial == [department]

        assert 'data-rights="programs_edit" data-attending="1">Врач' in html
        assert "programs_delete schedule group_schedule staff catalog" in html

    def test_deactivate(self, client, boss, doctor, department):
        client.force_login(boss)
        data = form_data(department, username="doctor", short_name="Иванова А.А.")
        del data["is_active"]

        client.post(reverse("users:edit", args=[doctor.pk]), data)

        doctor.refresh_from_db()
        assert not doctor.is_active
        assert User.objects.filter(pk=doctor.pk).exists(), "отключается, а не удаляется"


class TestSelfProtection:
    def test_cannot_drop_own_users_right(self, client, boss, department):
        client.force_login(boss)
        data = form_data(department, username="boss", rights=[Right.PROGRAMS_EDIT])

        response = client.post(reverse("users:edit", args=[boss.pk]), data)

        assert "Нельзя снять с себя" in response.content.decode()
        boss.refresh_from_db()
        assert Right.USERS in boss.rights

    def test_cannot_deactivate_self(self, boss, department):
        boss.is_active = False

        with pytest.raises(services.UsersError, match="отключить"):
            services.save_user(User.objects.get(pk=boss.pk), boss, departments=[department])

    def test_superuser_may_edit_self_freely(self, admin_user, department):
        admin_user.rights = []

        services.save_user(admin_user, admin_user, departments=[department])

        assert Membership.objects.filter(user=admin_user).exists()


class TestPassword:
    def test_set_password(self, client, boss, doctor):
        client.force_login(boss)
        url = reverse("users:password", args=[doctor.pk])

        response = client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})

        assert response.status_code == 302
        doctor.refresh_from_db()
        assert doctor.check_password(NEW_PASSWORD)
