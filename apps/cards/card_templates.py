"""Начальные шаблоны карт, которые лежат в репозитории.

В фазе 1 они станут начальными данными модели CardTemplate (шаблон на отделение и ШРМ).
"""

from pathlib import Path

from .xlsx.extract import TEMPLATE_FILE
from .xlsx.mapping import CardMapping, load_mappings

TEMPLATES_DIR = Path(__file__).resolve().parent / "resources" / "card_templates"
MAPPING_FILE = TEMPLATES_DIR / "mapping.json"


def bundled_mappings() -> dict[int, CardMapping]:
    return load_mappings(MAPPING_FILE)


def bundled_template(shrm: int) -> Path:
    return TEMPLATES_DIR / TEMPLATE_FILE.format(shrm=shrm)
