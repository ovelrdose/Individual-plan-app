from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.schedule_file import ScheduleFileError
from apps.catalog.services import UnknownGroupError, load_initial_catalog

PRIMARY_DOCS = settings.BASE_DIR / "primary_docs"


class Command(BaseCommand):
    help = "Заполняет справочники: отделение, группы ЛФК и бассейна, тренажёры, процедуры, слоты."

    def add_arguments(self, parser):
        parser.add_argument("--lfk", type=Path, default=PRIMARY_DOCS / "lfk.xlsx")
        parser.add_argument("--pool", type=Path, default=PRIMARY_DOCS / "basseyn.xlsx")

    def handle(self, *args, lfk: Path, pool: Path, **options):
        try:
            report = load_initial_catalog(lfk, pool)
        except (ScheduleFileError, UnknownGroupError, FileNotFoundError) as error:
            raise CommandError(str(error)) from error

        for what in sorted(set(report.created) | set(report.existing)):
            self.stdout.write(
                f"{what}: создано {report.created[what]}, уже было {report.existing[what]}"
            )
