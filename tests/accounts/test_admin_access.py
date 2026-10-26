from datetime import time

import pytest
from django.urls import reverse

from apps.accounts.models import Preset, Right, User
from apps.cards.models import CardExport
from apps.catalog.models import Procedure, ProcedureKind


class TestStaffFlag:
    """Админка Django — только у суперпользователя (решение 83): is_staff = is_superuser."""

    @pytest.mark.parametrize("rights", [[], [Right.CATALOG], [Right.USERS], list(Right)])
    def test_rights_do_not_give_admin(self, rehab, rights):
        rehab.rights = rights
        rehab.is_staff = True  # вручную тоже не держится
        rehab.save()
        rehab.refresh_from_db()

        assert not rehab.is_staff

    def test_superuser_stays_staff(self, admin_user):
        admin_user.save()

        assert admin_user.is_staff

    def test_superuser_opens_user_in_admin(self, client, admin_user, doctor):
        client.force_login(admin_user)

        url = reverse("admin:accounts_user_change", args=[doctor.pk])
        assert client.get(url).status_code == 200


class TestAdminOnlySuperuser:
    @pytest.mark.parametrize(
        "name",
        [
            "admin:index",
            "admin:catalog_procedure_changelist",
            "admin:staff_instructor_changelist",
            "admin:org_department_changelist",
            "admin:accounts_user_changelist",
        ],
    )
    def test_rehab_and_admin_preset_have_no_admin(self, client, rehab, make_user, department, name):
        boss = make_user("boss", (department, Preset.ADMIN))
        for user in (rehab, boss):
            client.force_login(user)
            response = client.get(reverse(name))
            # Не сотрудник админки — на страницу входа админки.
            assert response.status_code == 302 and "/admin/login/" in response["Location"]

    def test_navbar_admin_link_only_for_superuser(self, client, rehab, admin_user):
        client.force_login(rehab)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert "Справочники" in html and "Администрирование" not in html

        client.force_login(admin_user)
        html = client.get(reverse("home"), follow=True).content.decode()
        assert "Администрирование" in html

    @pytest.mark.parametrize("synonyms", ["ST – 150\nst 150\n\n", "ST – 150\r\nst 150"])
    def test_superuser_creates_procedure_with_synonyms(self, client, admin_user, synonyms):
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_procedure_add"),
            {
                "name": "Новая",
                "card_label": "Новая",
                "kind": ProcedureKind.CARD_ONLY,
                "synonyms": synonyms,
                "place": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert Procedure.objects.get(name="Новая").synonyms == ["ST – 150", "st 150"]

    def test_superuser_sees_group_sessions(self, client, admin_user, other_department):
        group = Procedure.objects.create(
            name="Чужая группа", card_label="Чужая", kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )  # fmt: skip
        group.sessions.create(start_time=time(9, 10))
        client.force_login(admin_user)

        html = client.get(
            reverse("admin:catalog_procedure_change", args=[group.pk])
        ).content.decode()

        assert "09:10" in html, "у группы показывается её расписание"


class TestDeleteUsers:
    """Журнал выгрузок удалению не мешает, лечащий врач защищён (TZ.md, решение 39)."""

    def test_admin_deletes_user_with_card_exports(
        self, client, admin_user, make_user, make_program, department
    ):
        program = make_program()
        nurse = make_user("nurse", (department, Preset.REHAB))
        CardExport.objects.create(program=program, user=nurse)
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:accounts_user_delete", args=[nurse.pk]), {"post": "yes"}
        )

        assert response.status_code == 302
        assert not User.objects.filter(username="nurse").exists()
        assert CardExport.objects.get().user is None

    def test_attending_doctor_cannot_be_deleted_silently(
        self, client, admin_user, doctor, make_program
    ):
        make_program()
        client.force_login(admin_user)

        response = client.get(reverse("admin:accounts_user_delete", args=[doctor.pk]))

        assert response.status_code == 200
        assert response.context["protected"], "админка показывает, что у врача есть программы"
        assert User.objects.filter(pk=doctor.pk).exists()
