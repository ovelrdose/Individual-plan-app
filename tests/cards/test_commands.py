from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

SOURCE = Path(settings.BASE_DIR) / "primary_docs" / "programm_template.xlsx"


@pytest.mark.skipif(not SOURCE.exists(), reason="нет исходного файла заказчика")
def test_extract_card_templates(tmp_path):
    call_command("extract_card_templates", source=SOURCE, out=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["shrm3.xlsx", "shrm4.xlsx", "shrm5.xlsx"]
