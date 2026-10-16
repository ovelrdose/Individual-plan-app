from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.access import SESSION_DEPARTMENT_KEY
from apps.accounts.models import Role
from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import (
    DuplicateProgramError,
    Plan,
    PreviousCoursesFound,
    find_duplicates,
    import_prescription_sheet,
)
from apps.exchange.views import PENDING_KEY
from apps.programs.models import Program, ProgramSource
from tests.sheets import make_doc_sheet, make_sheet

pytestmark = pytest.mark.usefixtures("full_catalog")

IMPORT_URL = reverse("exchange:sheet_import")
FORCE_URL = reverse("exchange:sheet_import_force")
NO_IB = "ФИО  {fio}      ШРМ 3          Палата № 9"

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

    def test_rehab_imports_with_attending_doctor(self, rehab, doctor, department):
        """Специалист ФР загружает лист за врача отделения (решение 75)."""
        program = import_prescription_sheet(rehab, department, sheet(), attending_doctor=doctor)

        assert program.attending_doctor == doctor

    def test_stranger_cannot_import(self, make_user, doctor, department, other_department):
        stranger = make_user("stranger", (other_department, Role.REHAB))
        with pytest.raises(PermissionDenied):
            import_prescription_sheet(stranger, department, sheet(), attending_doctor=doctor)

    def test_diagnosis_is_not_stored(self, client, doctor, department):
        """Диагноза нет в карте — в системе его тоже нет (TZ.md, решение 70)."""
        program = do_import(doctor, department, dx="Q99.9")
        client.force_login(doctor)

        page = client.get(reverse("programs:detail", args=[program.pk])).content.decode()

        assert "Q99.9" not in page and "Диагноз" not in page
        assert not hasattr(program, "diagnosis")

    def test_history_author(self, doctor, department):
        program = do_import(doctor, department)

        assert program.history.last().history_user == doctor
        assert program.prescriptions.get().history.first().history_user == doctor


def upload(name="лист.docx", **kwargs):
    return SimpleUploadedFile(name, make_sheet(**kwargs).getvalue())


def header(fio: str = "Тестова Анна Сергеевна", rest: str = "ИБ №5402  ШРМ 4  Палата № 5а") -> str:
    return f"ФИО  {fio}      {rest}"


def select(client, department) -> None:
    session = client.session
    session[SESSION_DEPARTMENT_KEY] = department.pk
    session.save()


@pytest.fixture
def two_department_doctor(make_user, department, other_department):
    return make_user("both", (department, Role.DOCTOR), (other_department, Role.DOCTOR))


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

    def test_upload_old_word(self, client, doctor):
        """Старый .doc читается так же, как .docx."""
        client.force_login(doctor)
        file = SimpleUploadedFile("лист.doc", make_doc_sheet(room="12б").getvalue())

        response = client.post(self.url, {"file": file})

        program = Program.objects.get()
        assert response.url == reverse("programs:detail", args=[program.pk])
        assert program.room == "12б"

    @pytest.mark.parametrize(
        ("file", "message"),
        [
            (SimpleUploadedFile("лист.pdf", b"x"), "в формате .docx или .doc"),
            (SimpleUploadedFile("лист.doc", b"x"), "не читается"),
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

    def test_rehab_chooses_doctor(self, client, rehab, doctor):
        client.force_login(rehab)

        assert "attending_doctor" in client.get(self.url).context["form"].fields
        without = client.post(self.url, {"file": upload()})
        assert "attending_doctor" in without.context["form"].errors

        client.post(self.url, {"file": upload(), "attending_doctor": doctor.pk})
        assert Program.objects.get().attending_doctor == doctor

    def test_user_without_role_has_no_access(self, client, make_user):
        client.force_login(make_user("nobody"))

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


class TestRecognition:
    """Как строки реальных листов становятся назначениями (FR-IMP-2…5)."""

    def test_groups_without_dashes(self, doctor, department, full_catalog):
        text = "Групповые занятия ЛФК\nЭрго общая\nI can нога\n30 мин 2 р/д е/д"

        program = do_import(doctor, department, rows=[("02.10.26", text, "")])

        assert labels(program) == ["Эрго общая", "I can нога"]

    def test_dosage_in_title_and_abbreviations(self, doctor, department, full_catalog):
        """Лист «ЛФК в группе, …, 4 раза в день» с сокращениями н/к, в/к (решение 78)."""
        text = (
            "ЛФК в группе, 30 минут, ежедневно, 4 раза в день\n"
            "Эрго общая\nI-can н/к\nI-can в/к\nЭрго в/к"
        )

        program = do_import(
            doctor,
            department,
            rows=[
                ("02.10.26", "Школа пациента", ""),
                ("02.10.26", text, ""),
                ("02.10.26", "ФТП", ""),
            ],
        )

        assert labels(program) == ["Эрго общая", "I can нога", "I can рука", "Эрго кисть"]
        assert {p.per_day for p in program.prescriptions.all()} == {1}
        assert program.import_warnings == []

    def test_groups_and_individual_in_one_cell(self, doctor, department, full_catalog):
        text = (
            "Групповые занятия ЛФК\n- Эрго общая\n30 мин 1 р/д\n"
            "Индивидуальное занятие ЛФК 30 мин 1 р/д"
        )

        program = do_import(doctor, department, rows=[("02.10.26", text, "")])

        assert labels(program) == ["Эрго общая", "Индивидуальное занятие"]

    def test_pool_with_group_as_bullet(self, doctor, department, full_catalog):
        text = "Бассейн\n- верхняя конечность\n30 мин 1 р/д"

        program = do_import(doctor, department, rows=[("02.10.26", text, "")])

        assert labels(program) == ["ЛФК в воде: верхняя конечность"]

    def test_consultations_in_physio_column(self, doctor, department, full_catalog):
        program = do_import(
            doctor, department, rows=[("02.10.26", "Консультация психолога, логопеда", "")]
        )

        assert labels(program) == ["Психолог", "Логопед"]

    def test_absurd_dosage_import(self, doctor, department):
        program = import_prescription_sheet(
            doctor,
            department,
            sheet(rows=[("02.10.26", "Имитрон 40000 мин", "")]),
            attending_doctor=doctor,
        )

        assert program.prescriptions.get().duration_min == 15, "длительность из справочника"
        assert "не похожа на правду" in program.import_warnings[0]


class TestDatesOnImport:
    """Даты назначений при импорте (FR-IMP-6, FR-PRG-3)."""

    def test_cancel_before_prescribed(self, doctor, department):
        program = do_import(
            doctor,
            department,
            rows=[("02.10.26", "Имитрон", ""), ("06.10.26", "Степпер", "04.10.26")],
        )
        stepper = program.prescriptions.get(raw_text="Степпер")

        assert (stepper.start_date, stepper.cancel_date) == (date(2026, 10, 6), None)
        assert any("«Степпер»: дата отмены" in w for w in program.import_warnings)

    def test_cancel_on_first_day(self, doctor, department):
        program = do_import(doctor, department, rows=[("02.10.26", "Имитрон", "02.10.26")])

        assert program.prescriptions.get().cancel_date is None
        assert any("дата отмены" in w for w in program.import_warnings)

    def test_cancel_on_last_day_is_kept(self, doctor, department):
        # ШРМ 4 с 02.10: курс до 16.10 включительно, отмена 16.10 допустима.
        program = do_import(doctor, department, rows=[("02.10.26", "Имитрон", "16.10.26")])

        assert program.prescriptions.get().cancel_date == date(2026, 10, 16)
        assert program.import_warnings == []

    def test_prescribed_after_course_end(self, doctor, department):
        program = do_import(
            doctor,
            department,
            shrm=3,
            rows=[("02.10.26", "Имитрон", ""), ("20.10.26", "Степпер", "")],
        )
        stepper = program.prescriptions.get(raw_text="Степпер")

        assert program.end_date == date(2026, 10, 12)
        assert stepper.start_date is None
        assert any("дата назначения вне курса" in w for w in program.import_warnings)

    @pytest.mark.parametrize(
        ("shrm", "end"),
        [(3, date(2026, 10, 12)), (4, date(2026, 10, 16)), (5, date(2026, 10, 21))],
    )
    def test_course_length_by_shrm(self, doctor, department, shrm, end):
        program = do_import(doctor, department, shrm=shrm)

        assert (program.start_date, program.end_date) == (date(2026, 10, 2), end)
        assert len(program.course_dates()) == {3: 11, 4: 15, 5: 20}[shrm]


class TestDuplicateEdges:
    """Границы повтора (FR-IMP-11): последний день курса, ручное продление, ФИО без ИБ."""

    def test_manual_program_without_ib_matches_by_name(self, make_program, department):
        make_program(
            full_name="Тестова Анна Сергеевна", history_number="", start_date=date(2026, 10, 2)
        )

        found = find_duplicates(
            department,
            sheet(fio="ТЕСТОВА Анна  Сергеевна".replace("  ", " ")),
            date(2026, 10, 2),
        )

        assert len(found) == 1

    def test_different_ib_is_another_course(self, make_program, department):
        make_program(
            full_name="Тестова Анна Сергеевна", history_number="999", start_date=date(2026, 10, 2)
        )

        assert find_duplicates(department, sheet(ib="5402"), date(2026, 10, 2)) == []

    def test_last_day_equals_first_day_is_duplicate(self, doctor, department):
        first = do_import(doctor, department, shrm=3)  # 02.10–12.10

        with pytest.raises(DuplicateProgramError) as error:
            do_import(doctor, department, rows=[("12.10.26", "Имитрон", "")])
        assert error.value.programs == [first]

    def test_next_day_after_course_is_not_duplicate(self, doctor, department):
        do_import(doctor, department, shrm=3)  # 02.10–12.10

        # Не дубль, а прошлый курс (FR-IMP-13).
        with pytest.raises(PreviousCoursesFound):
            do_import(doctor, department, rows=[("13.10.26", "Имитрон", "")])

    def test_new_course_ending_on_first_day_of_existing(self, doctor, department):
        do_import(doctor, department, rows=[("12.10.26", "Имитрон", "")])

        with pytest.raises(DuplicateProgramError):
            # ШРМ 3 с 02.10 — до 12.10, ровно первый день существующего курса.
            do_import(doctor, department, shrm=3)

    def test_manually_extended_course_counts(self, doctor, department):
        from apps.programs.services import save_program

        first = do_import(doctor, department, shrm=3)
        first.end_date = date(2026, 10, 20)
        save_program(doctor, first, end_date_changed=True)

        with pytest.raises(DuplicateProgramError):
            do_import(doctor, department, rows=[("18.10.26", "Имитрон", "")])

    def test_same_name_with_yo_without_ib(self, doctor, department):
        do_import(doctor, department, header=NO_IB.format(fio="Семёнов Пётр Иванович"))

        with pytest.raises(DuplicateProgramError):
            do_import(doctor, department, header=NO_IB.format(fio="Семенов Петр Иванович"))


class TestImportScreenEdges:
    """Ввод, который не должен давать 500 (FR-IMP-12)."""

    @pytest.mark.parametrize("bad", ["ШРМ 2", "ШРМ: 4", "ШРМ"])
    def test_bad_shrm_is_a_clear_error(self, client, doctor, bad):
        client.force_login(doctor)
        text = header(rest=f"ИБ №5402  {bad}  Палата № 5а")

        response = client.post(IMPORT_URL, {"file": upload(header=text)})

        assert response.status_code == 200
        assert "не найдено: ШРМ" in response.content.decode()

    def test_empty_file(self, client, doctor):
        client.force_login(doctor)

        response = client.post(IMPORT_URL, {"file": SimpleUploadedFile("лист.docx", b"")})

        assert response.status_code == 200
        assert response.context["form"].errors["file"]
        assert not Program.objects.exists()

    def test_upper_case_extension(self, client, doctor):
        client.force_login(doctor)

        response = client.post(IMPORT_URL, {"file": upload("ЛИСТ.DOCX")})

        assert response.status_code == 302
        assert Program.objects.count() == 1

    @pytest.mark.parametrize(
        "text",
        [
            header(rest="ИБ №5402  ШРМ 4  Палата № 5а,6б,7в,8г"),
            header(fio="Тестова " * 25 + "Сергеевна"),
        ],
        ids=["long-room", "long-name"],
    )
    def test_too_long_values(self, client, doctor, text):
        client.force_login(doctor)

        response = client.post(IMPORT_URL, {"file": upload(header=text)})

        assert response.status_code in (200, 302)

    def test_long_header_in_upload(self, client, doctor):
        client.force_login(doctor)
        response = client.post(IMPORT_URL, {"file": upload(room="12345678901234")})

        assert response.status_code == 200
        assert "слишком длинные" in response.content.decode()

    def test_only_consultations_without_physio(self, client, doctor):
        client.force_login(doctor)
        response = client.post(
            IMPORT_URL, {"file": upload(rows=[], diagnostics=["Консультация психолога"])}
        )

        program = Program.objects.get()
        assert response.url == reverse("programs:detail", args=[program.pk])
        assert labels(program) == ["Психолог"]
        assert "начало курса взято сегодняшним днём" in program.import_warnings[0]

    def test_duplicate_page_links_existing(self, client, doctor):
        client.force_login(doctor)
        client.post(IMPORT_URL, {"file": upload()})
        existing = Program.objects.get()

        response = client.post(IMPORT_URL, {"file": upload()})

        assert reverse("programs:detail", args=[existing.pk]) in response.content.decode()
        assert Program.objects.count() == 1


class TestImportDepartment:
    """Отделение выбирается явно на экране загрузки (TZ.md, решение 38)."""

    def test_department_defaults_to_header_and_can_be_changed(
        self, client, two_department_doctor, department, other_department
    ):
        client.force_login(two_department_doctor)
        client.post(reverse("switch_department"), {"department": other_department.pk})

        page = client.get(IMPORT_URL)
        assert page.context["form"]["department"].value() == other_department.pk

        client.post(IMPORT_URL, {"file": upload(), "department": department.pk})

        program = Program.objects.get()
        assert program.department == department
        assert client.session[SESSION_DEPARTMENT_KEY] == department.pk, "шапка переключилась"

    def test_department_without_role_is_not_offered(
        self, client, make_user, department, other_department
    ):
        user = make_user("mixed", (department, Role.DOCTOR))
        client.force_login(user)

        choices = list(client.get(IMPORT_URL).context["form"].fields["department"].queryset)
        response = client.post(IMPORT_URL, {"file": upload(), "department": other_department.pk})

        assert choices == [department]
        assert "department" in response.context["form"].errors
        assert not Program.objects.exists()

    def test_admin_picks_department_and_its_doctor(
        self, client, admin_user, doctor, make_user, department, other_department
    ):
        stranger = make_user("stranger", (other_department, Role.DOCTOR))
        client.force_login(admin_user)

        wrong = client.post(
            IMPORT_URL,
            {"file": upload(), "department": department.pk, "attending_doctor": stranger.pk},
        )
        assert "attending_doctor" in wrong.context["form"].errors

        client.post(
            IMPORT_URL,
            {"file": upload(), "department": department.pk, "attending_doctor": doctor.pk},
        )
        assert Program.objects.get().attending_doctor == doctor

    def test_doctor_with_two_departments_imports_into_selected(
        self, client, make_user, department, other_department
    ):
        user = make_user(
            "both", (department, Role.DOCTOR), (other_department, Role.DOCTOR), short_name="Д."
        )
        client.force_login(user)
        select(client, other_department)

        client.post(IMPORT_URL, {"file": upload()})

        program = Program.objects.get()
        assert (program.department, program.attending_doctor) == (other_department, user)

    def test_admin_without_membership_uses_selected_department(
        self, client, admin_user, make_user, department, other_department
    ):
        other_doctor = make_user("od", (other_department, Role.DOCTOR))
        client.force_login(admin_user)
        select(client, other_department)

        client.post(IMPORT_URL, {"file": upload(), "attending_doctor": other_doctor.pk})

        program = Program.objects.get()
        assert (program.department, program.attending_doctor) == (other_department, other_doctor)


class TestPendingSheets:
    """Разобранный лист ждёт в сессии, пока пользователь не решит про повтор."""

    def upload(self, client, fio, ib):
        file = SimpleUploadedFile("лист.docx", make_sheet(fio=fio, ib=ib).getvalue())
        return client.post(IMPORT_URL, {"file": file})

    def test_two_tabs_do_not_mix(self, client, doctor):
        client.force_login(doctor)
        self.upload(client, "Альфа Анна Сергеевна", "1")
        self.upload(client, "Бета Анна Сергеевна", "2")
        tab_a = self.upload(client, "Альфа Анна Сергеевна", "1")
        tab_b = self.upload(client, "Бета Анна Сергеевна", "2")

        client.post(FORCE_URL, {"token": tab_a.context["token"]})

        created = Program.objects.latest("pk")
        assert created.full_name == "Альфа Анна Сергеевна"
        assert tab_b.context["token"] in client.session[PENDING_KEY]

    @pytest.mark.parametrize(
        "pending",
        [
            {"department": 1, "doctor": 1, "sheet": {"unknown": 1}},
            {"department": 1, "doctor": 10**9, "sheet": {}},
            "мусор",
        ],
    )
    def test_stale_or_broken_session(self, client, doctor, department, pending):
        client.force_login(doctor)
        session = client.session
        session[PENDING_KEY] = {"tok": pending}
        session.save()

        response = client.post(FORCE_URL, {"token": "tok"}, follow=True)

        assert "Загрузка устарела" in response.content.decode()

    def _make_pending(self, client, doctor) -> str:
        client.force_login(doctor)
        client.post(IMPORT_URL, {"file": upload()})
        response = client.post(IMPORT_URL, {"file": upload()})
        assert PENDING_KEY in client.session
        return response.context["token"]

    def _tamper(self, client, token: str, **values) -> None:
        session = client.session
        session[PENDING_KEY][token].update(values)
        session.save()

    def test_force_twice_creates_one_program(self, client, doctor):
        token = self._make_pending(client, doctor)

        client.post(FORCE_URL, {"token": token})
        second = client.post(FORCE_URL, {"token": token}, follow=True)

        assert Program.objects.count() == 2
        assert "Загрузка устарела" in second.content.decode()

    def test_force_with_unknown_token(self, client, doctor):
        self._make_pending(client, doctor)

        response = client.post(FORCE_URL, {"token": "чужой"}, follow=True)

        assert "Загрузка устарела" in response.content.decode()
        assert Program.objects.count() == 1

    def test_force_after_switching_department_keeps_original(
        self, client, make_user, department, other_department
    ):
        user = make_user("both", (department, Role.DOCTOR), (other_department, Role.DOCTOR))
        select(client, department)
        token = self._make_pending(client, user)
        select(client, other_department)

        client.post(FORCE_URL, {"token": token})

        assert Program.objects.count() == 2
        assert set(Program.objects.values_list("department", flat=True)) == {department.pk}

    def test_tampered_department_of_stranger(self, client, doctor, other_department):
        token = self._make_pending(client, doctor)
        self._tamper(client, token, department=other_department.pk)

        response = client.post(FORCE_URL, {"token": token})

        assert response.status_code in (302, 404)
        assert Program.objects.count() == 1

    def test_tampered_department_where_user_has_no_role_is_refused(
        self, client, make_user, department, other_department
    ):
        user = make_user("mixed", (department, Role.DOCTOR))
        select(client, department)
        token = self._make_pending(client, user)
        self._tamper(client, token, department=other_department.pk)

        response = client.post(FORCE_URL, {"token": token})

        assert response.status_code in (302, 403, 404)
        assert not Program.objects.filter(department=other_department).exists()

    def test_tampered_doctor_of_other_department(self, client, doctor, make_user, other_department):
        stranger = make_user("od", (other_department, Role.DOCTOR))
        token = self._make_pending(client, doctor)
        self._tamper(client, token, doctor=stranger.pk)

        response = client.post(FORCE_URL, {"token": token}, follow=True)

        assert "врачом этого отделения" in response.content.decode()
        assert Program.objects.count() == 1


class TestDoctorVariantsOnImport:
    """Листы разных врачей (TZ.md, решение 71)."""

    def test_new_card_only_procedures(self, doctor, department):
        rows = [
            ("05.10.26", "Меркурий ( э/с) на предплечья, 10 минут, е/д.", ""),
            ("05.10.26", "Диамаг на оголовье 20 мин 1 р/д е/д.", ""),
            ("05.10.26", "ЧЭНС поясничного отдела, 1р/день, 10 мин.", ""),
            ("05.10.26", "Пневмокомпрессия верхних конечностей 30 минут", ""),
        ]

        program = do_import(doctor, department, rows=rows)

        assert labels(program) == ["Меркурий", "Диамаг", "ЧЭНС", "Пневмокомпрессия"]
        assert not program.prescriptions.filter(procedure=None).exists()

    def test_card_only_procedure_is_one_card_row(self, doctor, department):
        program = do_import(
            doctor,
            department,
            rows=[("05.10.26", "Индивид.психолог 30 мин 1 р/д по будням", "")],
            diagnostics=["Консультация медицинского психолога, логопеда"],
        )

        assert labels(program) == ["Психолог", "Логопед"]
        assert program.prescriptions.get(procedure__name="Психолог").raw_text.startswith(
            "Индивид.психолог"
        )

    def test_number_is_frequency_only_for_individual(self, doctor, department):
        program = do_import(
            doctor,
            department,
            rows=[
                ("05.10.26", "Индивидуальное занятие ЛФК 30 мин №2", ""),
                ("05.10.26", "Имитрон 15 мин №2", ""),
            ],
        )

        per_day = dict(program.prescriptions.values_list("procedure__name", "per_day"))
        assert per_day == {"Индивидуальное занятие": 2, "Имитрон": 1}

    def test_group_time_from_sheet(self, doctor, department):
        text = "Групповые занятия ЛФК:\nКолено в 15:00 – 179 каб.\nСпина в 16:30\n30 мин 2 р/д"

        program = do_import(doctor, department, rows=[("05.10.26", text, "")])

        knee = program.prescriptions.get(procedure__name="ДС: коленный сустав")
        back = program.prescriptions.get(procedure__name="ДС: спина")
        assert knee.preferred_time == time(15, 0)
        assert set(knee.bookings.values_list("start", flat=True)) == {time(15, 0)}
        assert back.preferred_time is None
        assert any("нет занятия в 16:30" in w for w in program.import_warnings)

    def test_typo_is_recognized_with_warning(self, doctor, department):
        program = do_import(doctor, department, diagnostics=["Консультация медицинского логпеда"])

        assert "Логопед" in labels(program)
        assert any("похоже на опечатку" in w for w in program.import_warnings)
