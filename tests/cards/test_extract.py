from pathlib import Path

import pytest
from django.conf import settings
from openpyxl import load_workbook

from apps.cards.card_templates import bundled_mappings, bundled_template
from apps.cards.xlsx.extract import data_cells, extract_templates

SOURCE = Path(settings.BASE_DIR) / "primary_docs" / "programm_template.xlsx"
MAPPINGS = bundled_mappings()


def assert_clean_template(path: Path, shrm: int) -> None:
    workbook = load_workbook(path)
    assert workbook.sheetnames == [f"ШРМ {shrm}"]
    sheet = workbook.active
    leftovers = {
        c: sheet[c].value for c in data_cells(MAPPINGS[shrm]) if sheet[c].value is not None
    }
    assert leftovers == {}, "в шаблоне остались примерные данные"
    assert len(sheet._images) == 1
    assert sheet.sheet_properties.pageSetUpPr.fitToPage
    assert sheet.print_area.endswith(f"${MAPPINGS[shrm].date_blocks[-1].last_procedure_row}")


@pytest.mark.skipif(not SOURCE.exists(), reason="нет исходного файла заказчика")
def test_extract_from_customer_file(tmp_path):
    paths = extract_templates(SOURCE, MAPPINGS, tmp_path)

    assert [p.name for p in paths] == ["shrm3.xlsx", "shrm4.xlsx", "shrm5.xlsx"]
    for shrm, path in zip((3, 4, 5), paths, strict=True):
        assert_clean_template(path, shrm)


@pytest.mark.parametrize("shrm", [3, 4, 5])
def test_bundled_templates_are_clean(shrm):
    assert_clean_template(bundled_template(shrm), shrm)
