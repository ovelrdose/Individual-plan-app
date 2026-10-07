"""Повторный курс (TZ.md FR-PRG-10, FR-IMP-13, решение 63).

ИБ при повторной госпитализации новый — прошлые курсы ищутся по ФИО. «Взять прошлый план» —
назначения без дат и группа бассейна; шапка — из нового листа/формы.
"""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.exchange.prescription_sheet import parse_sheet
from apps.exchange.services import (
    DuplicateProgramError,
    Plan,
    PreviousCoursesFound,
    compare_plans,
    import_prescription_sheet,
)
from apps.programs import services
from apps.programs.models import Prescription, Program, WithdrawalReason
from apps.programs.services import ProgramsError, add_prescription
from apps.scheduling import board_days
from tests.sheets import make_sheet

NAME = "Тестова Анна Сергеевна"
ROWS = [
    ("02.10.26", "Индивидуальное занятие ЛФК 30 мин 1 р/д е/д", ""),
    ("02.10.26", "ST – 150 15 мин 1 р/д е/д", ""),
]


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: date(2026, 11, 2))


def plan(program) -> list[tuple]:
    return [
        (p.procedure.name if p.procedure else p.raw_text, p.duration_min, p.per_day, p.start_date)
        for p in program.prescriptions.select_related("procedure").order_by("card_order")
    ]


class TestFindAndCopy:
    @pytest.fixture
    def old(self, doctor, make_program, procedures):
        program = make_program(full_name=NAME, start_date=date(2026, 9, 1), history_number="1")
        add_prescription(
            doctor,
            Prescription(
                program=program, procedure=procedures["equipment"], duration_min=15, per_day=1
            ),
        )
        add_prescription(
            doctor,
            Prescription(
                program=program,
                procedure=procedures["individual"],
                per_day=2,
                cancel_date=date(2026, 9, 10),
            ),
        )
        return program

    def test_found_by_name_ignoring_case_yo_and_spaces(self, old, department):
        found = services.find_previous_courses(
            department, "  тестова   анна сергеевна ", date(2026, 10, 1)
        )
        assert found == [old]

    def test_overlapping_and_other_department_are_not_previous(
        self, old, department, other_department
    ):
        assert services.find_previous_courses(department, NAME, date(2026, 9, 10)) == []
        assert services.find_previous_courses(other_department, NAME, date(2026, 10, 1)) == []

    def test_withdrawn_course_is_previous(self, old, rehab, department):
        services.withdraw(rehab, old, date(2026, 9, 5), WithdrawalReason.REFUSAL)
        assert services.find_previous_courses(department, NAME, date(2026, 9, 10)) == [old]

    def test_newest_first(self, old, make_program, department):
        newer = make_program(full_name=NAME, start_date=date(2026, 10, 1), history_number="2")
        assert services.find_previous_courses(department, NAME, date(2026, 11, 1)) == [
            newer,
            old,
        ]

    def test_copy_plan_without_dates(self, old, doctor, make_program):
        new = make_program(full_name=NAME, start_date=date(2026, 11, 2), history_number="2")

        services.link_previous(doctor, new, old, copy=True)

        new.refresh_from_db()
        assert new.previous == old
        assert plan(new) == [
            ("st-150", 15, 1, None),
            ("Индивидуальное занятие", None, 2, None),
        ]
        assert new.prescriptions.filter(cancel_date__isnull=False).count() == 0

    def test_copy_into_filled_program_is_refused(self, old, doctor, make_program, procedures):
        new = make_program(full_name=NAME, start_date=date(2026, 11, 2))
        add_prescription(doctor, Prescription(program=new, procedure=procedures["card_only"]))
        with pytest.raises(ProgramsError, match="уже есть назначения"):
            services.link_previous(doctor, new, old, copy=True)

    def test_courses_chain(self, old, doctor, make_program):
        second = make_program(full_name=NAME, start_date=date(2026, 10, 1))
        third = make_program(full_name=NAME, start_date=date(2026, 11, 2))
        services.link_previous(doctor, second, old, copy=False)
        services.link_previous(doctor, third, second, copy=False)
        assert services.patient_courses(Program.objects.get(pk=second.pk)) == [old, second, third]

    def test_rehab_cannot_link(self, old, rehab, make_program):
        new = make_program(full_name=NAME, start_date=date(2026, 11, 2))
        with pytest.raises(PermissionDenied):
            services.link_previous(rehab, new, old, copy=False)


class TestScreens:
    @pytest.fixture
    def old(self, doctor, make_program, procedures):
        program = make_program(full_name=NAME, start_date=date(2026, 9, 1))
        add_prescription(doctor, Prescription(program=program, procedure=procedures["equipment"]))
        return program

    def form(self, doctor, **extra) -> dict:
        return {
            "full_name": NAME,
            "sex": "Ж",
            "room": "7",
            "shrm": 4,
            "history_number": "77",
            "attending_doctor": doctor.pk,
            "start_date": "2026-11-02",
            **extra,
        }

    def test_manual_create_asks_then_copies(self, client, doctor, old):
        client.force_login(doctor)
        url = reverse("programs:create")

        asked = client.post(url, self.form(doctor))
        assert (
            "Пациент уже лечился" in asked.content.decode()
            and not Program.objects.filter(history_number="77").exists()
        )

        client.post(url, self.form(doctor, previous_choice=f"copy:{old.pk}"))
        new = Program.objects.get(history_number="77")
        assert new.previous == old and plan(new) == [("st-150", None, 1, None)]

    def test_manual_create_other_patient(self, client, doctor, old):
        client.force_login(doctor)
        client.post(reverse("programs:create"), self.form(doctor, previous_choice="other"))
        new = Program.objects.get(history_number="77")
        assert new.previous is None and not new.prescriptions.exists()

    def test_repeat_button_and_form(self, client, doctor, old):
        client.force_login(doctor)
        page = client.get(reverse("programs:detail", args=[old.pk])).content.decode()
        assert reverse("programs:repeat", args=[old.pk]) in page

        form = client.get(reverse("programs:repeat", args=[old.pk]))
        assert form.context["form"].initial["full_name"] == NAME
        assert form.context["previous_choice"] == f"copy:{old.pk}"

        client.post(
            reverse("programs:repeat", args=[old.pk]),
            self.form(doctor, previous_choice=f"copy:{old.pk}"),
        )
        new = Program.objects.get(history_number="77")
        assert new.previous == old and new.prescriptions.count() == 1
        detail = client.get(reverse("programs:detail", args=[new.pk])).content.decode()
        assert "Курсы пациента" in detail and "01.09.2026" in detail

    def test_no_repeat_for_current_course(self, client, doctor, make_program):
        current = make_program(start_date=date(2026, 10, 30))
        client.force_login(doctor)
        page = client.get(reverse("programs:detail", args=[current.pk])).content.decode()
        assert reverse("programs:repeat", args=[current.pk]) not in page
        response = client.get(reverse("programs:repeat", args=[current.pk]))
        assert response.url == reverse("programs:detail", args=[current.pk])


@pytest.mark.usefixtures("full_catalog")
class TestImport:
    def sheet(self, rows=ROWS, **kwargs):
        return parse_sheet(make_sheet(fio=NAME, rows=rows, **kwargs))

    @pytest.fixture
    def old(self, doctor, department):
        return import_prescription_sheet(
            doctor,
            department,
            self.sheet(rows=[("01.09.26", "ST – 150 15 мин 1 р/д е/д", "")], ib="1"),
            attending_doctor=doctor,
        )

    def test_asks_when_patient_was_here(self, old, doctor, department):
        with pytest.raises(PreviousCoursesFound) as error:
            import_prescription_sheet(
                doctor, department, self.sheet(ib="2"), attending_doctor=doctor
            )
        assert error.value.programs == [old]

    def test_by_new_sheet_links(self, old, doctor, department):
        new = import_prescription_sheet(
            doctor,
            department,
            self.sheet(ib="2"),
            attending_doctor=doctor,
            plan=Plan.SHEET,
            previous=old,
        )
        assert new.previous == old and new.history_number == "2"
        assert new.prescriptions.count() == 2

    def test_previous_plan_keeps_header_from_sheet(self, old, doctor, department):
        new = import_prescription_sheet(
            doctor,
            department,
            self.sheet(ib="2", room="12"),
            attending_doctor=doctor,
            plan=Plan.PREVIOUS,
            previous=old,
        )
        assert (new.room, new.history_number, new.start_date) == ("12", "2", date(2026, 10, 2))
        assert [p.procedure.name for p in new.prescriptions.all()] == ["st-150"]
        assert any("из прошлого курса" in w for w in new.import_warnings)

    def test_other_patient_and_missing_previous(self, old, doctor, department):
        new = import_prescription_sheet(
            doctor, department, self.sheet(ib="2"), attending_doctor=doctor, plan=Plan.OTHER
        )
        assert new.previous is None
        with pytest.raises(ValidationError):
            import_prescription_sheet(
                doctor, department, self.sheet(ib="3"), attending_doctor=doctor, plan=Plan.SHEET
            )

    def test_withdrawn_overlapping_is_previous_not_duplicate(self, doctor, rehab, department):
        old = import_prescription_sheet(
            doctor, department, self.sheet(ib="1"), attending_doctor=doctor
        )
        with pytest.raises(DuplicateProgramError):
            import_prescription_sheet(
                doctor,
                department,
                self.sheet(rows=[("08.10.26", "ST – 150 15 мин 1 р/д е/д", "")], ib="1"),
                attending_doctor=doctor,
            )
        services.withdraw(rehab, old, date(2026, 10, 5), WithdrawalReason.REFUSAL)
        with pytest.raises(PreviousCoursesFound):
            import_prescription_sheet(
                doctor,
                department,
                self.sheet(rows=[("08.10.26", "ST – 150 15 мин 1 р/д е/д", "")], ib="1"),
                attending_doctor=doctor,
            )

    def test_compare_plans(self, old, department):
        lines = compare_plans(
            department,
            self.sheet(rows=[*ROWS[:1], ("02.10.26", "ST – 150 20 мин 1 р/д е/д", "")]),
            old,
        )
        assert sorted(line.state for line in lines) == ["added", "changed"]
        changed = next(line for line in lines if line.state == "changed")
        assert (changed.old, changed.new) == ("15 мин, 1 р/д", "20 мин, 1 р/д")
        removed = compare_plans(department, self.sheet(rows=ROWS[:1]), old)
        assert {line.state for line in removed} == {"added", "removed"}

    def test_screen_flow(self, client, old, doctor):
        client.force_login(doctor)
        upload = SimpleUploadedFile("лист.docx", make_sheet(fio=NAME, rows=ROWS, ib="2").getvalue())

        page = client.post(reverse("exchange:sheet_import"), {"file": upload})
        text = page.content.decode()
        assert "Пациент уже лечился" in text and "Взять прошлый план" in text

        done = client.post(
            reverse("exchange:sheet_import_force"),
            {"token": page.context["token"], "plan": "previous", "previous": old.pk},
        )
        new = Program.objects.get(history_number="2")
        assert done.url == reverse("programs:detail", args=[new.pk])
        assert new.previous == old and new.prescriptions.count() == 1

    def test_screen_other_patient(self, client, old, doctor):
        client.force_login(doctor)
        upload = SimpleUploadedFile("лист.docx", make_sheet(fio=NAME, rows=ROWS, ib="2").getvalue())
        page = client.post(reverse("exchange:sheet_import"), {"file": upload})
        client.post(
            reverse("exchange:sheet_import_force"),
            {"token": page.context["token"], "plan": "other"},
        )
        new = Program.objects.get(history_number="2")
        assert new.previous is None and new.prescriptions.count() == 2
