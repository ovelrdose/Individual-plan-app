from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.cards.card_templates import bundled_mappings, bundled_template
from apps.cards.prototype import sample_card
from apps.cards.xlsx.writer import render_card


class Command(BaseCommand):
    help = "Выгружает прототипы карт ШРМ 3/4/5 на обезличенных данных (фаза 0)."

    def add_arguments(self, parser):
        parser.add_argument("--out", type=Path, default=settings.BASE_DIR / "out")

    def handle(self, *args, out: Path, **options):
        out.mkdir(parents=True, exist_ok=True)
        for shrm, mapping in sorted(bundled_mappings().items()):
            content = render_card(bundled_template(shrm), mapping, sample_card(shrm))
            path = out / f"Программа_прототип_ШРМ{shrm}.xlsx"
            path.write_bytes(content)
            self.stdout.write(f"Карта: {path}")
