"""Дни занятий: в день поступления и в день выписки занятий нет никаких (TZ.md, FR-SCH-1)."""

from datetime import date, time

from apps.catalog.models import GroupSession
from apps.programs.models import Prescription
from apps.programs.services import add_prescription
from apps.scheduling.manual import is_active_on


def test_groups_skip_admission_and_discharge(doctor, make_program, procedures):
    GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))
    program = make_program(shrm=3)  # 05.10–15.10
    add_prescription(doctor, Prescription(program=program, procedure=procedures["group"]))

    dates = sorted(program.bookings.values_list("date", flat=True))

    assert dates == program.therapy_dates()
    assert (dates[0], dates[-1]) == (date(2026, 10, 6), date(2026, 10, 14))


def test_manual_check_rejects_admission_and_discharge(make_program, procedures):
    program = make_program(shrm=3)
    prescription = Prescription.objects.create(program=program, procedure=procedures["group"])

    assert not is_active_on(prescription, program, date(2026, 10, 5))
    assert is_active_on(prescription, program, date(2026, 10, 6))
    assert not is_active_on(prescription, program, date(2026, 10, 15))
