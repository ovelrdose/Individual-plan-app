"""Инструкторы и их распорядок из шахматки заказчика — для демонстрации на настоящих фамилиях.

    .venv/bin/python manage.py load_board_instructors              # добавить / дополнить
    .venv/bin/python manage.py load_board_instructors --replace    # и удалить остальных

Из файла берутся только фамилии инструкторов, пары 2/2 и постоянные обязанности (см.
``apps.exchange.instructor_board``); записи пациентов не читаются. Смены: одиночки — 5/2, пары —
2/2 (первый в подписи работает с ``--since`` два дня, второй — следующие два); кто из пары когда
работает, в шахматке не видно — поправить можно на экране «Смены». Повторный запуск безопасен.

``--replace`` удаляет инструкторов, которых нет в файле, вместе с их сменами, распорядком,
блоками, сводками перестроек и индивидуальными занятиями, а затем пересобирает расписание
текущих программ — занятия встают к новым инструкторам.
"""

from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.catalog.models import GroupSession, InstructorSlot, Procedure, ProcedureKind
from apps.exchange.instructor_board import BoardStaff, normalize_group, read_board_staff
from apps.programs.models import Program
from apps.scheduling.models import Booking
from apps.scheduling.services import replan
from apps.staff.models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
    ShiftPatternKind,
)
from apps.staff.services import set_partner


class Command(BaseCommand):
    help = "Загрузить инструкторов, пары 2/2 и постоянный распорядок из шахматки (.xlsx)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=str(Path(settings.BASE_DIR) / "primary_docs" / "instructions_schedule.xlsx"),
        )
        parser.add_argument(
            "--since",
            type=date.fromisoformat,
            default=None,
            help="С какой даты действуют смены и распорядок (ГГГГ-ММ-ДД), по умолчанию — сегодня.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Удалить инструкторов, которых нет в файле, с их сменами и занятиями.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"Нет файла {path}.")
        since = options["since"] or date.today()
        staff = read_board_staff(path, _groups())
        self.stdout.write(
            f"Разобрано листов с текущим составом: {staff.sheets} "
            f"({staff.first_sheet} … {staff.last_sheet})."
        )
        with transaction.atomic():
            names = self._load(staff, since)
            removed = self._remove_others(names) if options["replace"] else []
        if removed:
            self.stdout.write(f"Удалены: {', '.join(removed)}.")
        programs = Program.objects.filter(end_date__gte=since).order_by("pk")
        for program in programs:
            replan(program)
        self.stdout.write(
            self.style.SUCCESS(
                f"Инструкторы: {', '.join(names)}. Пересобрано программ: {programs.count()}."
            )
        )

    def _load(self, staff: BoardStaff, since: date) -> list[str]:
        slots = {slot.start: slot for slot in InstructorSlot.objects.all()}
        names: list[str] = []
        order = 10
        for column in staff.columns:
            people = []
            for name in column.members:
                item, _created = Instructor.objects.get_or_create(
                    short_name=name, defaults={"full_name": name, "display_order": order}
                )
                if not item.is_active or item.display_order != order:
                    item.is_active, item.display_order = True, order
                    item.save(update_fields=["is_active", "display_order"])
                people.append(item)
                names.append(name)
                order += 10
            if len(people) == 2:
                set_partner(people[0], people[1])
            for index, person in enumerate(people):
                if not person.shift_patterns.exists():
                    pair = len(people) == 2
                    ShiftPattern.objects.create(
                        instructor=person,
                        pattern=ShiftPatternKind.TWO_TWO if pair else ShiftPatternKind.FIVE_TWO,
                        anchor_date=since + timedelta(days=2 * index),
                        valid_from=since,
                    )
                for duty in column.duties:
                    slot = slots.get(duty.slot)
                    if slot is None or person.duties.filter(slot=slot).exists():
                        continue
                    session = None
                    if duty.kind == "GROUP_LEAD":
                        session = GroupSession.objects.filter(
                            procedure__name=duty.group,
                            procedure__department=None,
                            start_time=duty.slot,
                        ).first()
                        if session is None:
                            continue
                    item = InstructorDuty(
                        instructor=person,
                        slot=slot,
                        kind=duty.kind,
                        group_session=session,
                        valid_from=since,
                    )
                    item.full_clean()
                    item.save()
            for person in people:
                duties = ", ".join(
                    f"{d.slot.start:%H:%M} {d.text}" for d in person.duties.select_related("slot")
                )
                self.stdout.write(f"  {person.team_label}: {person.short_name} — {duties or '—'}")
        return names

    def _remove_others(self, names: list[str]) -> list[str]:
        others = list(Instructor.objects.exclude(short_name__in=names))
        if not others:
            return []
        ids = [item.pk for item in others]
        Booking.objects.filter(instructor_id__in=ids).delete()
        InstructorBlock.objects.filter(instructor_id__in=ids).delete()
        InstructorDuty.objects.filter(instructor_id__in=ids).delete()
        ShiftException.objects.filter(instructor_id__in=ids).delete()
        ShiftPattern.objects.filter(instructor_id__in=ids).delete()
        Instructor.objects.filter(pk__in=ids).update(partner=None)
        Instructor.objects.filter(pk__in=ids).delete()
        return [item.short_name for item in others]


def _groups() -> dict[str, str]:
    """Нормализованное название или синоним группы ЛФК → название в справочнике."""
    result: dict[str, str] = {}
    for item in Procedure.objects.filter(kind=ProcedureKind.LFK_GROUP, department=None):
        for name in [item.name, *(item.synonyms or [])]:
            result[normalize_group(name)] = item.name
    return result
