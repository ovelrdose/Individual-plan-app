from datetime import time

import pytest
from django.urls import reverse

from apps.accounts.models import Preset, Right, User
from apps.cards.models import CardExport
from apps.catalog.models import Procedure, ProcedureKind


class TestStaffFlag:
    """Вход в админку — по правам «Справочники» и «Пользователи и отделения» (решение 79)."""

    def test_rehab_gets_admin_without_groups(self, rehab):
        rehab.refresh_from_db()

        assert rehab.is_staff
        assert not rehab.groups.exists() and not rehab.user_permissions.exists()

    def test_doctor_has_no_admin(self, doctor):
        doctor.refresh_from_db()

        assert not doctor.is_staff

    @pytest.mark.parametrize(
        ("rights", "staff"),
        [([], False), ([Right.SCHEDULE], False), ([Right.CATALOG], True), ([Right.USERS], True)],
    )
    def test_follows_rights_on_save(self, rehab, rights, staff):
        rehab.rights = rights
        rehab.save()
        rehab.refresh_from_db()

        assert rehab.is_staff is staff

    def test_manual_flag_is_overwritten(self, doctor):
        doctor.is_staff = True
        doctor.save()
        doctor.refresh_from_db()

        assert not doctor.is_staff

    def test_superuser_opens_user_in_admin(self, client, admin_user, doctor):
        client.force_login(admin_user)

        url = reverse("admin:accounts_user_change", args=[doctor.pk])
        assert client.get(url).status_code == 200

    def test_superuser_stays_staff(self, admin_user):
        admin_user.save()

        assert admin_user.is_staff


class TestRehabInAdmin:
    def test_sees_catalog_only(self, client, rehab):
        client.force_login(rehab)

        assert client.get(reverse("admin:catalog_procedure_changelist")).status_code == 200
        assert client.get(reverse("admin:staff_instructor_changelist")).status_code == 200
        assert client.get(reverse("admin:catalog_instructorslot_changelist")).status_code == 200
        assert client.get(reverse("admin:org_department_changelist")).status_code == 403
        assert client.get(reverse("admin:accounts_user_changelist")).status_code == 403
        url = reverse(
            "admin:catalog_procedure_delete",
            args=[
                Procedure.objects.create(name="X", card_label="X", kind=ProcedureKind.CARD_ONLY).pk
            ],
        )
        assert client.get(url).status_code == 403, "удаление — только у суперпользователя"
        assert client.post(reverse("admin:catalog_instructorslot_add")).status_code == 403

    def test_users_right_opens_departments(self, client, make_user, department):
        boss = make_user("boss", (department, Preset.DOCTOR), rights=[Right.USERS])
        client.force_login(boss)

        assert client.get(reverse("admin:org_department_changelist")).status_code == 200
        assert client.get(reverse("admin:catalog_procedure_changelist")).status_code == 403

    def test_navbar_link_is_named_catalog(self, client, rehab):
        client.force_login(rehab)

        html = client.get(reverse("home"), follow=True).content.decode()

        assert "Справочники" in html

    def test_procedures_of_other_departments_are_hidden(
        self, client, rehab, department, other_department
    ):
        common = Procedure.objects.create(
            name="Общая", card_label="Общая", kind=ProcedureKind.CARD_ONLY
        )
        own = Procedure.objects.create(
            name="Своя", card_label="Своя", kind=ProcedureKind.CARD_ONLY, department=department
        )
        foreign = Procedure.objects.create(
            name="Чужая",
            card_label="Чужая",
            kind=ProcedureKind.CARD_ONLY,
            department=other_department,
        )
        client.force_login(rehab)

        listed = set(
            client.get(reverse("admin:catalog_procedure_changelist")).context["cl"].queryset
        )

        assert listed == {common, own}
        url = reverse("admin:catalog_procedure_change", args=[foreign.pk])
        assert client.get(url).status_code == 302  # «объект не найден» → на список

    @pytest.mark.parametrize("synonyms", ["ST – 150\nst 150\n\n", "ST – 150\r\nst 150"])
    def test_create_procedure_with_synonyms(self, client, rehab, synonyms):
        client.force_login(rehab)

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


class TestRehabGroupSessions:
    @pytest.fixture
    def groups(self, department, other_department):
        own = Procedure.objects.create(
            name="Своя группа",
            card_label="Своя",
            kind=ProcedureKind.LFK_GROUP,
            department=department,
        )
        foreign = Procedure.objects.create(
            name="Чужая группа",
            card_label="Чужая",
            kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )
        card_only = Procedure.objects.create(
            name="Массаж", card_label="Массаж", kind=ProcedureKind.CARD_ONLY
        )
        for group in (own, foreign):
            group.sessions.create(start_time=time(9, 10))
        return own, foreign, card_only

    def test_list_and_choices(self, client, rehab, groups):
        own, _, _ = groups
        client.force_login(rehab)

        listed = client.get(reverse("admin:catalog_groupsession_changelist")).context["cl"].queryset
        choices = (
            client.get(reverse("admin:catalog_groupsession_add"))
            .context["adminform"]
            .form.fields["procedure"]
            .queryset
        )

        assert {s.procedure for s in listed} == {own}
        assert set(choices) == {own}

    def test_procedure_page_shows_synonyms_and_sessions(self, client, rehab, groups):
        own, _, card_only = groups
        own.synonyms = ["Первый", "Второй"]
        own.save()
        client.force_login(rehab)

        html = client.get(reverse("admin:catalog_procedure_change", args=[own.pk])).content.decode()
        form = client.get(reverse("admin:catalog_procedure_add")).context["adminform"].form
        card_only_page = client.get(reverse("admin:catalog_procedure_change", args=[card_only.pk]))

        assert "Первый\nВторой" in html
        assert "09:10" in html, "у группы показывается её расписание"
        assert not card_only_page.context["inline_admin_formsets"]
        assert {d for d in form.fields["department"].queryset} == {
            rehab.memberships.get().department
        }


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
