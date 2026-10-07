from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import (
    DuplicateProgramError,
    Plan,
    PreviousCoursesFound,
    import_prescription_sheet,
)
from apps.exchange.views import PENDING_KEY
from apps.programs.models import Program, ProgramSource
from tests.sheets import make_sheet

pytestmark = pytest.mark.usefixtures("full_catalog")

GROUPS = "Групповые занятия ЛФК\n- Эрго общая\n- I can нога\n30 мин 2 р/д е/д"
CONSULTATIONS = (
    "Консультация терапевта, медицинского психолога, медицинского логопеда, эрготерапевта"
)


def sheet(**kwargs):
    return parse_sheet(make_sheet(**kwargs))


def do_import(doctor, department, **kwargs):
    force = kwargs.pop("force", False)
    return import_prescription_sheet(
        doctor, department, sheet(**kwargs), attending_doctor=doctor, force=force
    )


def labels(program):
    return [p.label for p in program.prescriptions.all()]


class TestImportService:
    def test_creates_program_with_prescriptions(self, doctor, department):
        program = do_import(
            doctor,
            department,
            shrm=4,
            rows=[
                ("02.10.26", GROUPS, ""),
                ("02.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", ""),
                ("02.10.26", "ST – 150 15 мин 1 р/д е/д", ""),
                ("02.10.26", "Алмаг на заднюю поверхность бедра, Алмаг 20 мин, 1 р/д е/д", ""),
            ],
            diagnostics=[CONSULTATIONS],
        )

        assert program.source == ProgramSource.IMPORT
        assert (program.full_name, program.room, program.shrm, program.history_number) == (
            "Тестова Анна Сергеевна",
            "5а",
            4,
            "5402",
        )
        assert program.sex == "Ж"
        assert program.age is None
        assert (program.start_date, program.end_date) == (date(2026, 10, 2), date(2026, 10, 16))
        assert program.attending_doctor == doctor
        assert labels(program) == [
            "Эрго общая",
            "I can нога",
            "Индивидуальное занятие",
            "st-150",
            "Алмаг",
            "Психолог",
            "Логопед",
            "Эрготерапевт",
        ]
        first = program.prescriptions.first()
        assert (first.duration_min, first.per_day, first.raw_text) == (
            30,
            1,
            "Групповые занятия ЛФК: Эрго общая",
        )
        assert program.import_warnings == []

    def test_dates_of_later_and_cancelled_rows(self, doctor, department):
        program = do_import(
            doctor,
            department,
            rows=[
                ("02.10.26", "Имитрон 15 мин", ""),
                ("05.10.26", "Степпер 15 мин", "09.10.26"),
            ],
        )
        imitron, stepper = program.prescriptions.all()

        assert (imitron.start_date, imitron.cancel_date) == (None, None)
        assert (stepper.start_date, stepper.cancel_date) == (date(2026, 10, 5), date(2026, 10, 9))

    def test_default_duration_from_catalog(self, doctor, department):
        program = do_import(doctor, department, rows=[("02.10.26", "Имитрон", "")])

        assert program.prescriptions.get().duration_min == 15

    def test_unrecognized_row_is_kept(self, doctor, department):
        program = do_import(
            doctor, department, rows=[("02.10.26", "Неизвестная процедура 10 мин", "")]
        )
        item = program.prescriptions.get()

        assert item.procedure is None
        assert item.raw_text == "Неизвестная процедура 10 мин"
        assert item.duration_min == 10

    def test_warnings(self, doctor, department):
        program = do_import(
            doctor,
            department,
            fio="Тестов Тест",
            second_header={"ib": "999"},
            rows=[
                ("02.10.26", "Индивидуальное занятие 30 мин 3 р/д", ""),
                ("02.10.26", "Имитрон", "30.12.26"),
            ],
        )

        warnings = " | ".join(program.import_warnings)
        assert "«ИБ»: 5402, 999" in warnings
        assert "Пол не определился" in warnings
        assert "3 р/д больше лимита — поставлено 2" in warnings
        assert "дата отмены после окончания курса" in warnings
        individual, imitron = program.prescriptions.all()
        assert individual.per_day == 2
        assert imitron.cancel_date is None
        assert program.sex == ""

    def test_no_dates_start_today(self, doctor, department):
        program = import_prescription_sheet(
            doctor,
            department,
            sheet(rows=[("", "Имитрон", "")]),
            attending_doctor=doctor,
            today=date(2026, 11, 1),
        )

        assert program.start_date == date(2026, 11, 1)
        assert "начало курса взято сегодняшним днём" in program.import_warnings[0]

    def test_duplicate_by_history_number(self, doctor, department):
        first = do_import(doctor, department, ib="5402")

        with pytest.raises(DuplicateProgramError) as error:
            do_import(doctor, department, ib="5402", fio="Другая Фамилия Ивановна")
        assert error.value.programs == [first]

        do_import(doctor, department, ib="5402", force=True)
        assert Program.objects.count() == 2

    def test_duplicate_by_name_without_history_number(self, doctor, department):
        header = "ФИО  Тестов Тест Тестович      ШРМ 3          Палата № 9"
        do_import(doctor, department, header=header)

        with pytest.raises(DuplicateProgramError):
            do_import(doctor, department, header=header.replace("Тестов", "ТЕСТОВ"))

    def test_not_duplicate_when_courses_do_not_overlap(self, doctor, department):
        do_import(doctor, department, rows=[("02.10.26", "Имитрон", "")])

        # Не дубль, а прошлый курс (FR-IMP-13): спрашиваем, по какому плану создать.
        with pytest.raises(PreviousCoursesFound):
            do_import(doctor, department, rows=[("01.12.26", "Имитрон", "")])
        import_prescription_sheet(
            doctor,
            department,
            sheet(rows=[("01.12.26", "Имитрон", "")]),
            attending_doctor=doctor,
            plan=Plan.OTHER,
        )

        assert Program.objects.count() == 2

    def test_other_department_is_not_duplicate(
        self, make_user, doctor, department, other_department
    ):
        other_doctor = make_user("other", (other_department, Role.DOCTOR))
        do_import(other_doctor, other_department)

        do_import(doctor, department)

    def test_rehab_cannot_import(self, rehab, doctor, department):
        with pytest.raises(PermissionDenied):
            import_prescription_sheet(rehab, department, sheet(), attending_doctor=doctor)

    def test_history_author(self, doctor, department):
        program = do_import(doctor, department)

        assert program.history.last().history_user == doctor
        assert program.prescriptions.get().history.first().history_user == doctor


def upload(name="лист.docx", **kwargs):
    return SimpleUploadedFile(name, make_sheet(**kwargs).getvalue())


class TestImportScreens:
    url = reverse("exchange:sheet_import")

    def test_upload_creates_and_opens_program(self, client, doctor):
        client.force_login(doctor)

        page = client.get(self.url)
        response = client.post(self.url, {"file": upload(diagnostics=[CONSULTATIONS])})

        program = Program.objects.get()
        assert "attending_doctor" not in page.context["form"].fields
        assert response.url == reverse("programs:detail", args=[program.pk])
        detail = client.get(response.url).content.decode()
        assert "Программа создана из листа назначений" in detail
        assert "Психолог" in detail

    @pytest.mark.parametrize(
        ("file", "message"),
        [
            (SimpleUploadedFile("лист.doc", b"x"), "в формате .docx"),
            (SimpleUploadedFile("лист.docx", b"not a zip"), "не читается"),
            (
                SimpleUploadedFile("лист.docx", make_sheet(with_table=False).getvalue()),
                "Физиотерапия",
            ),
        ],
    )
    def test_bad_files(self, client, doctor, file, message):
        client.force_login(doctor)

        response = client.post(self.url, {"file": file})

        assert response.status_code == 200
        assert message in response.content.decode()
        assert not Program.objects.exists()

    def test_too_big(self, client, doctor, monkeypatch):
        monkeypatch.setattr("apps.exchange.forms.MAX_FILE_SIZE", 10)
        client.force_login(doctor)

        response = client.post(self.url, {"file": upload()})

        assert "больше 5 МБ" in response.content.decode()

    def test_duplicate_then_force(self, client, doctor):
        client.force_login(doctor)
        client.post(self.url, {"file": upload()})

        duplicate = client.post(self.url, {"file": upload()})
        assert "Похожая программа уже есть" in duplicate.content.decode()
        assert PENDING_KEY in client.session

        forced = client.post(
            reverse("exchange:sheet_import_force"), {"token": duplicate.context["token"]}
        )
        assert Program.objects.count() == 2
        assert forced.url == reverse("programs:detail", args=[Program.objects.latest("pk").pk])
        assert client.session[PENDING_KEY] == {}

    def test_force_without_pending(self, client, doctor):
        client.force_login(doctor)

        response = client.post(reverse("exchange:sheet_import_force"), follow=True)

        assert "Загрузка устарела" in response.content.decode()

    def test_admin_chooses_doctor(self, client, admin_user, doctor):
        client.force_login(admin_user)

        without = client.post(self.url, {"file": upload()})
        assert "attending_doctor" in without.context["form"].errors

        client.post(self.url, {"file": upload(), "attending_doctor": doctor.pk})
        assert Program.objects.get().attending_doctor == doctor

    def test_rehab_has_no_access(self, client, rehab):
        client.force_login(rehab)

        assert client.get(self.url).status_code == 403


class TestEveningIndividual:
    """Мото-Л / Артромот — одно из индивидуальных, вечером, без строки в карте (FR-SCH-8)."""

    def test_moto_l_is_recognized_without_card_row(self, doctor, department):
        program = do_import(
            doctor,
            department,
            rows=[
                ("02.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", ""),
                ("02.10.26", "Мото-Л 30 мин 1 р/д", ""),
                ("02.10.26", "Артромот 20 мин", ""),
            ],
        )

        rows = {p.procedure.name: p for p in program.prescriptions.select_related("procedure")}
        assert rows["Мото-Л"].procedure.evening_individual
        assert rows["Артромот"].procedure.evening_individual
        assert not rows["Мото-Л"].in_card
        assert not rows["Артромот"].in_card
        assert rows["Индивидуальное занятие"].in_card
        assert not program.bookings.filter(procedure__evening_individual=True).exists()


class TestDayHospitalGroups:
    """Группы ДС из листа назначений ставятся по расписанию групп, как группы ЛФК (FR-IMP-14)."""

    def test_groups_from_list_get_sessions(self, doctor, department):
        from apps.catalog.models import Procedure, ProcedureKind
        from apps.scheduling.models import Booking, BookingKind

        if not Procedure.objects.filter(kind=ProcedureKind.DS_GROUP).exists():
            pytest.skip("нет файла групп ДС")
        program = do_import(
            doctor,
            department,
            rows=[
                ("02.10.26", "Групповые занятия ЛФК\n- спина\n- ШОП\n30 мин 2 р/д е/д", ""),
                ("02.10.26", "Массаж плечевого сустава", ""),
            ],
        )

        assert labels(program) == ["ДС: спина", "ДС: ШОП", "Массаж"]
        bookings = Booking.objects.filter(program=program, kind=BookingKind.DS_GROUP)
        assert {b.procedure.name for b in bookings} == {"ДС: спина", "ДС: ШОП"}
        # Каждый день занятий — по одному занятию каждой группы.
        days = (program.end_date - program.start_date).days - 1
        assert bookings.count() == 2 * days
