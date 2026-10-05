from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from apps.accounts.models import Role
from apps.programs import services
from apps.programs.models import Prescription, Program
from apps.programs.services import Period, ProgramsError


def add(doctor, program, procedure, **fields):
    return services.add_prescription(
        doctor, Prescription(program=program, procedure=procedure, **fields)
    )


def labels(program):
    return [p.label for p in program.prescriptions.all()]


class TestEndDate:
    @pytest.mark.parametrize(
        ("shrm", "expected"),
        [(3, date(2026, 10, 15)), (4, date(2026, 10, 19)), (5, date(2026, 10, 24))],
    )
    def test_computed_from_shrm(self, make_program, shrm, expected):
        program = make_program(shrm=shrm)

        assert program.end_date == expected
        assert not program.end_date_manual

    def test_recomputed_when_shrm_changes(self, make_program, doctor):
        program = make_program(shrm=3)
        program.shrm = 5

        services.save_program(doctor, program, end_date_changed=False)

        assert program.end_date == date(2026, 10, 24)

    def test_manual_date_is_kept_and_clearing_recomputes(self, make_program, doctor):
        program = make_program(shrm=4)
        program.end_date = date(2026, 10, 22)
        services.save_program(doctor, program, end_date_changed=True)
        program.shrm = 3
        services.save_program(doctor, program, end_date_changed=False)
        assert (program.end_date, program.end_date_manual) == (date(2026, 10, 22), True)

        program.end_date = None
        services.save_program(doctor, program, end_date_changed=True)
        assert (program.end_date, program.end_date_manual) == (date(2026, 10, 15), False)

    def test_entering_computed_date_is_not_manual(self, make_program, doctor):
        program = make_program(shrm=4)
        program.end_date = date(2026, 10, 19)

        services.save_program(doctor, program, end_date_changed=True)

        assert not program.end_date_manual

    def test_end_before_start_is_rejected(self, make_program, doctor):
        program = make_program()
        program.end_date = date(2026, 10, 1)

        with pytest.raises(ValidationError):
            services.save_program(doctor, program, end_date_changed=True)

    def test_course_cannot_lose_prescription_dates(self, make_program, doctor, procedures):
        program = make_program(shrm=4)
        add(
            doctor,
            program,
            procedures["group"],
            start_date=date(2026, 10, 15),
            cancel_date=date(2026, 10, 17),
        )

        program.end_date = date(2026, 10, 10)
        with pytest.raises(ProgramsError, match="Эрго общая"):
            services.save_program(doctor, program, end_date_changed=True)

        program.end_date = date(2026, 10, 17)
        services.save_program(doctor, program, end_date_changed=True)

    def test_optional_fields(self, make_program):
        program = make_program(sex="", age=None, history_number="", diagnosis="")

        program.full_clean()


class TestRights:
    def test_rehab_cannot_create_or_change(
        self, make_program, rehab, doctor, procedures, department
    ):
        program = make_program()
        program.room = "10"

        with pytest.raises(PermissionDenied):
            services.save_program(rehab, program, end_date_changed=False)
        with pytest.raises(PermissionDenied):
            add(rehab, program, procedures["group"])
        with pytest.raises(PermissionDenied):
            services.dismiss_warnings(rehab, program)
        assert not services.can_edit(rehab, program)
        assert not services.can_create(rehab, department)

    def test_attending_doctor_must_be_doctor_of_department(self, make_program, rehab):
        with pytest.raises(ProgramsError, match="Лечащий врач"):
            make_program(attending_doctor=rehab)

    def test_foreign_program_is_404(self, make_program, make_user, other_department):
        stranger = make_user("stranger", (other_department, Role.DOCTOR))

        with pytest.raises(Http404):
            services.get_program_or_404(stranger, make_program().pk)

    def test_admin_can_edit(self, make_program, admin_user):
        assert services.can_edit(admin_user, make_program())


class TestList:
    def test_periods_search_and_sort(self, make_program, doctor, department, procedures):
        make_program(full_name="Бета Б.Б.", room="12", history_number="555")
        make_program(full_name="Семёнов С.С.", room="5а", start_date=date(2026, 9, 1))
        gamma = make_program(full_name="Гамма Г.Г.", room="9")
        Prescription.objects.create(program=gamma, raw_text="что-то непонятное")
        today = date(2026, 10, 6)

        def names(**kwargs):
            return [
                p.full_name
                for p in services.programs_for(doctor, department, today=today, **kwargs)
            ]

        assert names() == ["Гамма Г.Г.", "Бета Б.Б."]
        assert names(period=Period.FINISHED) == ["Семёнов С.С."]
        assert names(period=Period.ALL) == ["Семёнов С.С.", "Гамма Г.Г.", "Бета Б.Б."]
        assert names(period=Period.ALL, query="семенов") == ["Семёнов С.С."]
        assert names(query="555") == ["Бета Б.Б."]
        counts = {
            p.full_name: p.unrecognized_count
            for p in services.programs_for(doctor, department, today=today)
        }
        assert counts == {"Гамма Г.Г.": 1, "Бета Б.Б.": 0}

    def test_other_department_is_not_listed(
        self, make_program, make_user, other_department, department
    ):
        make_program()
        stranger = make_user("stranger", (other_department, Role.DOCTOR))

        assert services.programs_for(stranger, department) == []


class TestReview:
    def test_items_and_dismiss(self, make_program, doctor):
        program = make_program()
        program.import_warnings = ["Разные ИБ"]
        program.save()
        Prescription.objects.create(program=program, raw_text="непонятное")

        assert services.review_items(program) == [
            "Разные ИБ",
            "Не распознано назначений: 1 — выберите для них процедуру (строки подсвечены).",
        ]
        services.dismiss_warnings(doctor, program)
        program.refresh_from_db()
        assert program.import_warnings == []
        assert len(services.review_items(program)) == 1, "нераспознанные строки остаются"


class TestPrescriptions:
    def test_add_move_delete(self, make_program, doctor, procedures):
        program = make_program()
        items = [
            add(doctor, program, procedures[key]) for key in ("group", "equipment", "card_only")
        ]

        services.move_prescription(doctor, items[2], -1)
        assert labels(program) == ["Эрго общая", "Эрготерапевт", "st-150"]
        services.move_prescription(doctor, items[0], -1)  # уже первая
        services.delete_prescription(doctor, items[1])
        services.move_prescription(doctor, items[2], -1)
        assert labels(program) == ["Эрготерапевт", "Эрго общая"]
        assert list(program.prescriptions.values_list("card_order", flat=True)) == [1, 2]

    def test_choose_procedure_for_unrecognized(self, make_program, doctor, procedures):
        program = make_program()
        item = Prescription.objects.create(program=program, raw_text="Тигo 15 мин")
        item.procedure = procedures["card_only"]

        services.update_prescription(doctor, item)

        assert Prescription.objects.get(pk=item.pk).procedure == procedures["card_only"]

    def test_individual_limit(self, make_program, doctor, procedures):
        program = make_program()
        add(doctor, program, procedures["individual"], per_day=2)

        with pytest.raises(ValidationError) as error:
            add(doctor, program, procedures["individual"], per_day=3)
        assert "per_day" in error.value.message_dict

    @pytest.mark.parametrize(
        ("start", "cancel", "field"),
        [
            (date(2026, 10, 4), None, "start_date"),
            (date(2026, 10, 20), None, "start_date"),
            (None, date(2026, 10, 5), "cancel_date"),
            (None, date(2026, 10, 20), "cancel_date"),
        ],
    )
    def test_invalid_dates(self, make_program, doctor, procedures, start, cancel, field):
        with pytest.raises(ValidationError) as error:
            add(doctor, make_program(), procedures["group"], start_date=start, cancel_date=cancel)
        assert field in error.value.message_dict

    @pytest.mark.parametrize(
        "action",
        [
            lambda user, item: services.update_prescription(user, item),
            lambda user, item: services.delete_prescription(user, item),
            lambda user, item: services.move_prescription(user, item, -1),
        ],
    )
    def test_actions_on_deleted_row(self, make_program, doctor, procedures, action):
        item = add(doctor, make_program(), procedures["group"])
        Prescription.objects.filter(pk=item.pk).delete()

        with pytest.raises(ProgramsError, match="уже удалено"):
            action(doctor, item)
        assert not Prescription.objects.filter(pk=item.pk).exists()

    def test_history_author(self, make_program, doctor, procedures):
        item = add(doctor, make_program(), procedures["group"])

        assert item.history.first().history_user == doctor


def test_program_is_finished(make_program):
    program = make_program(shrm=3)  # 05.10–15.10

    assert not program.is_finished(date(2026, 10, 15))
    assert program.is_finished(date(2026, 10, 16))
    assert isinstance(program, Program)
