from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.cards.card_templates import TEMPLATES_DIR, bundled_mappings
from apps.cards.xlsx.extract import extract_templates

DEFAULT_SOURCE = settings.BASE_DIR / "primary_docs" / "programm_template.xlsx"


class Command(BaseCommand):
    help = "Выделяет шаблоны карт ШРМ 3/4/5 из programm_template.xlsx и стирает примерные данные."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        parser.add_argument("--out", type=Path, default=TEMPLATES_DIR)

    def handle(self, *args, source: Path, out: Path, **options):
        for path in extract_templates(source, bundled_mappings(), out):
            self.stdout.write(f"Шаблон: {path}")
