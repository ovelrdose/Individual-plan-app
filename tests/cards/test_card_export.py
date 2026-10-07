from datetime import date, datetime
from io import BytesIO

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Role
from apps.cards.models import CardExport
from apps.cards.services import build_card_data, card_filename
from apps.cards.xlsx.formatting import sex_age_text
from apps.cards.xlsx.writer import CardRenderError
from apps.programs.models import Prescription


@pytest.fixture
def program(make_program, procedures):
    program = make_program(shrm=3, sex="Ж", age=17)  # 05.10–15.10, 11 дат
    Prescription.objects.create(program=program, procedure=procedures["group"], card_order=1)
    Prescription.objects.create(
        program=program,
        procedure=procedures["card_only"],
        card_order=2,
        start_date=date(2026, 10, 7),
        cancel_date=date(2026, 10, 10),
    )
    Prescription.objects.create(
        program=program, procedure=procedures["equipment"], card_order=3, in_card=False
    )
    return program


def test_card_data(program):
    data = build_card_data(program)

    assert (data.full_name, data.sex, data.age, data.doctor) == (
        "Тестов Тест Тестович",
        "Ж",
        17,
        "Иванова А.А.",
    )
    assert len(data.course_dates) == 11
    assert [(p.label, p.start_date, p.cancel_date) for p in data.procedures] == [
        ("Группа Эрго общая", None, None),
        ("Эрготерапевт", date(2026, 10, 7), date(2026, 10, 10)),
    ]
    assert data.schedule == ()


def test_unrecognized_rows_block_export(program):
    Prescription.objects.create(program=program, raw_text="Непонятная строка", card_order=4)

    with pytest.raises(CardRenderError, match="Непонятная строка"):
        build_card_data(program)


def test_unrecognized_row_outside_card_does_not_block(program):
    Prescription.objects.create(program=program, raw_text="Непонятная строка", in_card=False)

    build_card_data(program)


@pytest.mark.parametrize(
    ("sex", "age", "expected"),
    [("Ж", 17, "(Ж) 17 лет"), ("М", None, "(М)"), ("", 52, "52 года"), ("", None, "")],
)
def test_sex_age_text(sex, age, expected):
    assert sex_age_text(sex, age) == expected


def test_filename(program):
    assert card_filename(program) == "Программа_Тестов_100_05.10.2026.xlsx"
    program.history_number = ""
    assert card_filename(program) == "Программа_Тестов_05.10.2026.xlsx"


def test_download(client, doctor, program):
    client.force_login(doctor)

    response = client.get(reverse("cards:download", args=[program.pk]))

    assert response.status_code == 200
    assert "filename*=UTF-8''%D0%9F" in response["Content-Disposition"]
    sheet = load_workbook(BytesIO(response.content)).active
    assert sheet["I10"].value == "Тестов Тест Тестович"
    assert sheet["I11"].value == "(Ж) 17 лет"
    assert isinstance(sheet["C17"].value, datetime)
    assert [sheet["A18"].value, sheet["A19"].value, sheet["A20"].value] == [
        "Группа Эрго общая",
        "Эрготерапевт",
        None,
    ]
    assert sheet["C19"].fill.fill_type == "solid", "Эрготерапевт ещё не начат 05.10"
    assert CardExport.objects.get().user == doctor


def test_download_error_returns_to_program(client, doctor, program):
    Prescription.objects.create(program=program, raw_text="Непонятная строка", card_order=4)
    client.force_login(doctor)

    response = client.get(reverse("cards:download", args=[program.pk]), follow=True)

    assert response.redirect_chain[-1][0] == reverse("programs:detail", args=[program.pk])
    assert "нераспознанных назначений" in response.content.decode()
    assert not CardExport.objects.exists()


def test_rehab_can_download_stranger_cannot(client, rehab, make_user, other_department, program):
    client.force_login(rehab)
    assert client.get(reverse("cards:download", args=[program.pk])).status_code == 200

    client.force_login(make_user("stranger", (other_department, Role.DOCTOR)))
    assert client.get(reverse("cards:download", args=[program.pk])).status_code == 404


def test_card_schedule_follows_slot_grid(program):
    from datetime import time

    from apps.catalog.models import InstructorSlot

    InstructorSlot.objects.create(start=time(9, 10), end=time(9, 40))
    InstructorSlot.objects.create(start=time(9, 50), end=time(10, 20))
    InstructorSlot.objects.create(start=time(18, 0), end=time(18, 30), is_evening=True)

    data = build_card_data(program)

    # Расписание ещё не подобрано — в карте только окна дневной сетки, вечерних слотов нет.
    assert [(item.start, item.label) for item in data.schedule] == [
        (time(9, 10), ""),
        (time(9, 50), ""),
    ]


def test_card_marks_admission_and_discharge_as_rest_dates(program):
    data = build_card_data(program)

    assert data.rest_dates == {date(2026, 10, 5), date(2026, 10, 15)}
