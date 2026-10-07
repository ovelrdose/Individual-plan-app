"""Правки владельца от 04.10: видимость программ отделения, расписание — у специалиста ФР,
удаление программ и пользователей администратором, печать продлённого курса."""

from datetime import date
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.access import SESSION_DEPARTMENT_KEY
from apps.accounts.models import Role, User
from apps.cards.models import CardExport
from apps.cards.services import choose_template, export_card
from apps.cards.xlsx.writer import CardRenderError
from apps.programs.models import Prescription, Program
from apps.scheduling.models import Booking
from tests.sheets import make_sheet

IMPORT = reverse("exchange:sheet_import")


def upload(**kwargs):
    return SimpleUploadedFile("лист.docx", make_sheet(**kwargs).getvalue())


@pytest.fixture
def two_department_doctor(make_user, department, other_department):
    return make_user("both", (department, Role.DOCTOR), (other_department, Role.DOCTOR))


@pytest.mark.usefixtures("full_catalog")
class TestImportDepartment:
    def test_department_defaults_to_header_and_can_be_changed(
        self, client, two_department_doctor, department, other_department
    ):
        client.force_login(two_department_doctor)
        client.post(reverse("switch_department"), {"department": other_department.pk})

        page = client.get(IMPORT)
        assert page.context["form"]["department"].value() == other_department.pk

        client.post(IMPORT, {"file": upload(), "department": department.pk})

        program = Program.objects.get()
        assert program.department == department
        assert client.session[SESSION_DEPARTMENT_KEY] == department.pk, "шапка переключилась"

    def test_department_where_user_is_not_doctor_is_not_offered(
        self, client, make_user, department, other_department
    ):
        user = make_user("mixed", (department, Role.DOCTOR), (other_department, Role.REHAB))
        client.force_login(user)

        choices = list(client.get(IMPORT).context["form"].fields["department"].queryset)
        response = client.post(IMPORT, {"file": upload(), "department": other_department.pk})

        assert choices == [department]
        assert "department" in response.context["form"].errors
        assert not Program.objects.exists()

    def test_rehab_only_has_no_import(self, client, rehab):
        client.force_login(rehab)

        assert client.get(IMPORT).status_code == 403

    def test_admin_picks_department_and_its_doctor(
        self, client, admin_user, doctor, make_user, department, other_department
    ):
        stranger = make_user("stranger", (other_department, Role.DOCTOR))
        client.force_login(admin_user)

        wrong = client.post(
            IMPORT, {"file": upload(), "department": department.pk, "attending_doctor": stranger.pk}
        )
        assert "attending_doctor" in wrong.context["form"].errors

        client.post(
            IMPORT, {"file": upload(), "department": department.pk, "attending_doctor": doctor.pk}
        )
        assert Program.objects.get().attending_doctor == doctor


class TestDepartmentStaffSeePrograms:
    def test_rehab_sees_program_created_by_doctor(self, client, rehab, make_program):
        program = make_program(start_date=date.today())
        client.force_login(rehab)

        assert program.full_name in client.get(reverse("programs:list")).content.decode()
        detail = client.get(reverse("programs:detail", args=[program.pk]))
        assert detail.status_code == 200
        assert program.department.name in detail.content.decode()
        assert client.get(reverse("cards:download", args=[program.pk])).status_code == 200


class TestDeleteProgram:
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


class TestDeleteUsers:
    def test_admin_deletes_user_with_card_exports(
        self, client, admin_user, make_user, make_program, department
    ):
        program = make_program()
        nurse = make_user("nurse", (department, Role.REHAB))
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


class TestExtendedCourseCard:
    @pytest.mark.parametrize(
        ("shrm", "days", "expected"),
        [(3, 11, 3), (3, 13, 4), (4, 15, 4), (4, 18, 5), (3, 20, 5), (5, 20, 5), (5, 10, 5)],
    )
    def test_choose_template(self, shrm, days, expected):
        assert choose_template(shrm, days) == expected

    def test_longer_than_any_template(self):
        with pytest.raises(CardRenderError, match="Дней курса 25"):
            choose_template(4, 25)

    def test_shrm4_extended_prints_on_shrm5_template(self, doctor, make_program, procedures):
        program = make_program(shrm=4)  # 05.10–19.10
        program.end_date = date(2026, 10, 22)  # продлили до 18 дней
        from apps.programs.services import save_program

        save_program(doctor, program, end_date_changed=True)
        Prescription.objects.create(program=program, procedure=procedures["group"])

        _, content = export_card(doctor, program)
        sheet = load_workbook(BytesIO(content)).active

        assert sheet.title == "ШРМ 5"
        assert sheet["C18"].value.date() == date(2026, 10, 5)
        assert sheet["J31"].value.date() == date(2026, 10, 22), "18-я дата во втором блоке"
        assert sheet["K31"].value is None
