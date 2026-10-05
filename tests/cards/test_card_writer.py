from dataclasses import replace
from datetime import date, datetime, time
from io import BytesIO

import pytest
from openpyxl import load_workbook

from apps.cards.card_templates import bundled_mappings, bundled_template
from apps.cards.prototype import sample_card
from apps.cards.xlsx.data import CardProcedure, ScheduleItem
from apps.cards.xlsx.writer import DATE_FORMAT, TIME_FORMAT, CardRenderError, render_card

MAPPINGS = bundled_mappings()


def render(shrm, data=None):
    data = data or sample_card(shrm)
    content = render_card(bundled_template(shrm), MAPPINGS[shrm], data)
    return load_workbook(BytesIO(content)).active


def is_grey(cell):
    return cell.fill.fill_type == "solid" and cell.fill.start_color.rgb == "FFD9D9D9"


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_header(shrm):
    mapping, data = MAPPINGS[shrm], sample_card(shrm)
    sheet = render(shrm)

    assert sheet[mapping.department].value == "ОМР № 4"
    assert sheet[mapping.full_name].value == data.full_name
    assert sheet[mapping.sex_age].value.startswith(f"({data.sex}) {data.age} ")
    assert sheet[mapping.doctor].value == data.doctor


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_schedule_rows_with_windows_use_excel_time(shrm):
    area, data = MAPPINGS[shrm].schedule, sample_card(shrm)
    sheet = render(shrm)

    rows = list(area.rows)
    assert len(data.schedule) == len(rows), "прототип заполняет всю сетку"
    for row, item in zip(rows, data.schedule, strict=True):
        time_cell = sheet[f"{area.time_col}{row}"]
        assert isinstance(time_cell.value, time), "время — и у занятия, и у окна"
        assert time_cell.value == item.start
        assert time_cell.number_format == TIME_FORMAT
        assert sheet[f"{area.label_col}{row}"].value == (item.label or None)
        assert sheet[f"{area.place_col}{row}"].value == (item.place or None)


def test_windows_stay_empty():
    area = MAPPINGS[3].schedule
    sheet = render(3)

    # 9:50 — окно: время есть, процедуры и места нет.
    assert sheet[f"{area.time_col}5"].value == time(9, 50)
    assert sheet[f"{area.label_col}5"].value is None
    assert sheet[f"{area.place_col}5"].value is None


def test_short_schedule_leaves_rows_empty():
    area, data = MAPPINGS[4].schedule, sample_card(4)
    sheet = render(4, replace(data, schedule=data.schedule[:3]))

    assert sheet[f"{area.time_col}{area.rows[3]}"].value is None


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_dates_are_excel_dates_split_across_blocks(shrm):
    mapping, data = MAPPINGS[shrm], sample_card(shrm)
    sheet = render(shrm)

    written = []
    for block in mapping.date_blocks:
        for column in block.date_columns:
            cell = sheet[f"{column}{block.date_row}"]
            assert isinstance(cell.value, datetime)
            assert cell.number_format == DATE_FORMAT
            written.append(cell.value.date())
    assert written == list(data.course_dates)


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_procedures_and_grey_days(shrm):
    mapping, data = MAPPINGS[shrm], sample_card(shrm)
    sheet = render(shrm)

    dates = iter(data.course_dates)
    for block in mapping.date_blocks:
        block_dates = [next(dates) for _ in block.date_columns]
        for row, procedure in zip(block.procedure_rows, data.procedures, strict=False):
            assert sheet[f"A{row}"].value == procedure.label
            for column, day in zip(block.date_columns, block_dates, strict=True):
                cell = sheet[f"{column}{row}"]
                assert cell.value is None, "клетки под датами — для отметок от руки"
                assert is_grey(cell) == (not procedure.is_active(day))


def test_grey_days_reach_second_block_of_shrm5():
    sheet = render(5)
    # «Имитрон» (5-я строка) отменён с 14-го дня: во второй таблице это 4-я дата и далее.
    assert not is_grey(sheet["E36"])
    assert is_grey(sheet["F36"])
    assert is_grey(sheet["L36"])


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_template_design_is_kept(shrm):
    sheet = render(shrm)

    assert len(sheet._images) == 1, "логотип"
    assert sheet.print_area is not None
    assert sheet.sheet_properties.pageSetUpPr.fitToPage
    assert sheet.page_setup.orientation == "landscape"
    assert sheet["B3"].value == "Начало занятия"


def test_short_course_leaves_remaining_dates_empty():
    data = sample_card(4)
    data = replace(data, course_dates=data.course_dates[:10])
    sheet = render(4, data)

    assert sheet["L17"].value.date() == data.course_dates[-1]
    assert sheet["M17"].value is None


def test_too_many_schedule_rows():
    data = sample_card(3)
    extra = (ScheduleItem(time(17, 0), ""),)
    with pytest.raises(CardRenderError, match="11 строк"):
        render_card(bundled_template(3), MAPPINGS[3], replace(data, schedule=data.schedule + extra))


def test_too_many_procedures():
    data = sample_card(5)
    extra = (CardProcedure("Лишняя процедура"),)
    with pytest.raises(CardRenderError, match="Процедур для карты 13"):
        render_card(
            bundled_template(5), MAPPINGS[5], replace(data, procedures=data.procedures + extra)
        )


def test_too_many_dates():
    data = sample_card(3)
    with pytest.raises(CardRenderError, match="Дней курса 12"):
        render_card(
            bundled_template(3),
            MAPPINGS[3],
            replace(data, course_dates=(*data.course_dates, date(2026, 10, 16))),
        )


def test_procedure_active_range():
    procedure = CardProcedure("Алмаг", start_date=date(2026, 10, 7), cancel_date=date(2026, 10, 9))
    assert not procedure.is_active(date(2026, 10, 6))
    assert procedure.is_active(date(2026, 10, 7))
    assert procedure.is_active(date(2026, 10, 8))
    assert not procedure.is_active(date(2026, 10, 9))
    assert CardProcedure("Массаж").is_active(date(2026, 10, 6))
