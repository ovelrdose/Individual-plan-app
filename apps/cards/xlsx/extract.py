"""Подготовка шаблонов карт из файла заказчика (TZ.md, FR-CRD-1).

Из `programm_template.xlsx` каждый лист «ШРМ N» сохраняется отдельным файлом,
а примерные данные (пациент, врач, расписание, даты, процедуры) стираются по mapping.
В исходном файле нет масштаба печати — шаблону задаётся «вписать в 1 страницу».
"""

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from .mapping import CardMapping

SOURCE_SHEET = "ШРМ {shrm}"
TEMPLATE_FILE = "shrm{shrm}.xlsx"
CHAR_WIDTH = 1.2


def extract_templates(
    source: Path, mappings: dict[int, CardMapping], target_dir: Path
) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for shrm, mapping in sorted(mappings.items()):
        workbook = load_workbook(source)
        keep = SOURCE_SHEET.format(shrm=shrm)
        for sheet in list(workbook.worksheets):
            if sheet.title != keep:
                workbook.remove(sheet)
        workbook.active = 0
        # В файле заказчика стоит ручной пересчёт — он «прилипает» к сеансу Excel пользователя.
        workbook.calculation.calcMode = "auto"
        clear_data(workbook.active, mapping)
        set_print_layout(workbook.active, mapping)

        path = target_dir / TEMPLATE_FILE.format(shrm=shrm)
        workbook.save(path)
        written.append(path)
    return written


def data_cells(mapping: CardMapping) -> list[str]:
    """Все ячейки, которые заполняет выгрузка, — в шаблоне они должны быть пустыми."""
    cells = [mapping.department, mapping.full_name, mapping.sex_age, mapping.doctor]
    area = mapping.schedule
    for row in area.rows:
        cells += [f"{col}{row}" for col in _columns_between(area.time_col, area.place_col)]
    for block in mapping.date_blocks:
        cells += [f"{col}{block.date_row}" for col in block.date_columns]
        last_col = block.date_columns[-1]
        for row in block.procedure_rows:
            cells += [
                f"{col}{row}" for col in _columns_between(mapping.procedure_label_col, last_col)
            ]
    return cells


def clear_data(sheet: Worksheet, mapping: CardMapping) -> None:
    for coordinate in data_cells(mapping):
        cell = sheet[coordinate]
        if not isinstance(cell, MergedCell):
            cell.value = None


def set_print_layout(sheet: Worksheet, mapping: CardMapping) -> None:
    """Область печати — от A1 до правого нижнего края карты, вся карта на одном листе A4."""
    last_col, last_row = _card_bounds(sheet, mapping)
    sheet.print_area = f"A1:{get_column_letter(last_col)}{last_row}"
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 1
    sheet.print_options.horizontalCentered = True


def _card_bounds(sheet: Worksheet, mapping: CardMapping) -> tuple[int, int]:
    """Край карты: ячейки с текстом или рамкой, логотипы и ячейки, которые заполнит выгрузка."""
    last_col = last_row = 1
    for row in sheet.iter_rows():
        for cell in row:
            border = cell.border
            has_border = any(
                getattr(border, side).style for side in ("left", "right", "top", "bottom")
            )
            if cell.value is not None or has_border:
                last_col, last_row = max(last_col, cell.column), max(last_row, cell.row)
            if isinstance(cell.value, str):
                last_col = max(last_col, _text_end_column(sheet, cell))
    for image in sheet._images:
        # Индексы якоря считаются с нуля.
        last_col = max(last_col, image.anchor.to.col + 1)
        last_row = max(last_row, image.anchor.to.row + 1)
    for coordinate in data_cells(mapping):
        cell = sheet[coordinate]
        last_col, last_row = max(last_col, cell.column), max(last_row, cell.row)
    return last_col, last_row


def _text_end_column(sheet: Worksheet, cell) -> int:
    """Колонка, до которой Excel дотягивает текст, вылезающий из ячейки вправо.

    Оценка грубая (символ Calibri ≈ 1,2 единицы ширины колонки при шрифте 11),
    но достаточная, чтобы область печати не обрезала заголовки.
    """
    merged = any(cell.coordinate in rng for rng in sheet.merged_cells.ranges)
    if (
        merged
        or cell.alignment.wrap_text
        or cell.alignment.horizontal not in (None, "general", "left")
    ):
        return cell.column
    need = len(cell.value.rstrip()) * CHAR_WIDTH * (cell.font.sz or 11) / 11
    column = cell.column
    while True:
        need -= sheet.column_dimensions[get_column_letter(column)].width or 8.43
        if need <= 0:
            return column
        column += 1


def _columns_between(first: str, last: str) -> list[str]:
    start, end = column_index_from_string(first), column_index_from_string(last)
    return [get_column_letter(index) for index in range(start, end + 1)]
