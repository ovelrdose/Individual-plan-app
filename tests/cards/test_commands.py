from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

SOURCE = Path(settings.BASE_DIR) / "primary_docs" / "programm_template.xlsx"


def test_card_prototype(tmp_path):
    call_command("card_prototype", out=tmp_path)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"Программа_прототип_ШРМ{shrm}.xlsx" for shrm in (3, 4, 5)]


@pytest.mark.skipif(not SOURCE.exists(), reason="нет исходного файла заказчика")
def test_extract_card_templates(tmp_path):
    call_command("extract_card_templates", source=SOURCE, out=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["shrm3.xlsx", "shrm4.xlsx", "shrm5.xlsx"]
