from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction
from django.db.models import Q, QuerySet

from apps.org.models import Department

from . import initial_data as data
from .domain import normalize_name
from .models import (
    SESSION_KINDS,
    Equipment,
    GroupSession,
    InstructorSlot,
    Procedure,
    ProcedureKind,
    Section,
)
from .schedule_file import read_schedule


@dataclass
class LoadReport:
    created: Counter = field(default_factory=Counter)
    existing: Counter = field(default_factory=Counter)

    def count(self, what: str, created: bool) -> None:
        (self.created if created else self.existing)[what] += 1


class UnknownGroupError(ValueError):
    pass


@transaction.atomic
def load_initial_catalog(
    lfk_path: Path, pool_path: Path, ds_path: Path | None = None
) -> LoadReport:
    """Заполняет справочники (FR-CAT-6). Идемпотентна: недостающее создаёт,
    существующее не трогает — правки, сделанные в админке, не затираются.
    ``ds_path`` — расписание групп дневного стационара; без него группы ДС не загружаются."""
    report = LoadReport()

    _, created = Department.objects.get_or_create(
        code=data.DEPARTMENT_CODE, defaults={"name": data.DEPARTMENT_NAME}
    )
    report.count("отделения", created)

    changed = _load_groups(lfk_path, data.LFK_GROUPS, report)
    changed |= _load_groups(pool_path, data.POOL_GROUPS, report)
    if ds_path is not None:
        changed |= _load_groups(ds_path, data.DS_GROUPS, report)
    _get_or_create_procedure(data.POOL_UNSPECIFIED, report)
    _get_or_create_procedure(data.INDIVIDUAL, report)
    for spec in data.CARD_ONLY:
        _get_or_create_procedure(spec, report)

    for spec in data.EQUIPMENT:
        equipment, created = Equipment.objects.get_or_create(name=spec.name)
        report.count("тренажёры", created)
        _get_or_create_procedure(
            data.ProcedureSpec(
                spec.name,
                spec.name,
                ProcedureKind.EQUIPMENT,
                equipment.duration_min,
                data.EQUIPMENT_PLACE,
                spec.synonyms,
            ),
            report,
            equipment=equipment,
        )

    for start, end, is_evening in data.SLOTS:
        _, created = InstructorSlot.objects.get_or_create(
            start=start, defaults={"end": end, "is_evening": is_evening}
        )
        report.count("слоты инструкторов", created)

    if changed:
        # Новое занятие группы — новое возможное время: текущие программы с этой группой
        # пересобираются, как после правки в «Групповых занятиях» (решение 81).
        from apps.scheduling.services import replan_for_group

        replan_for_group(*sorted(changed))
    return report


def _load_groups(path: Path, specs: tuple[data.ProcedureSpec, ...], report: LoadReport) -> set[int]:
    """Группы и их занятия из файла; возвращает группы, у которых появились занятия."""
    rows = read_schedule(path)
    unknown = [f"{row.cell} «{row.name}»" for row in rows if not _spec_for(row.name, specs)]
    if unknown:
        raise UnknownGroupError(
            f"{path.name}: неизвестные группы — {', '.join(unknown)}. "
            "Добавьте их описание в apps/catalog/initial_data.py."
        )
    procedures = {spec.name: _get_or_create_procedure(spec, report) for spec in specs}
    changed: set[int] = set()
    for row in rows:
        spec = _spec_for(row.name, specs)
        procedure = procedures[spec.name]
        _, created = GroupSession.objects.get_or_create(
            procedure=procedure,
            start_time=row.start,
            defaults={
                "duration_min": spec.duration_min or 30,
                # Место группы — в справочнике, у занятия — только если отличается.
                "place": row.place if row.place != procedure.place else "",
            },
        )
        report.count("занятия групп", created)
        if created:
            changed.add(procedure.pk)
    return changed


def _spec_for(name: str, specs: tuple[data.ProcedureSpec, ...]) -> data.ProcedureSpec | None:
    return next((spec for spec in specs if spec.matches(name)), None)


def _get_or_create_procedure(
    spec: data.ProcedureSpec, report: LoadReport, equipment: Equipment | None = None
) -> Procedure:
    existing = Procedure.objects.filter(name__iexact=spec.name, department=None).first()
    if existing is not None:
        report.count("процедуры", False)
        return existing
    # Новая группа попадает в первый раздел своего вида; раздела нет — заводится (решение 82).
    # Уже заведённые не трогаем: раздел могли поменять на экране.
    is_group = spec.kind in SESSION_KINDS and not spec.group_choice
    procedure, created = Procedure.objects.get_or_create(
        name=spec.name,
        department=None,
        defaults={
            "section": Section.default_for(spec.kind) if is_group else None,
            "card_label": spec.card_label,
            "kind": spec.kind,
            "default_duration_min": spec.duration_min,
            "place": spec.place,
            "synonyms": list(spec.synonyms),
            "equipment": equipment,
            "group_choice": spec.group_choice,
            "evening_individual": spec.evening_individual,
        },
    )
    report.count("процедуры", created)
    return procedure


def visible_procedures(department: Department) -> QuerySet[Procedure]:
    """Действующие процедуры центра и данного отделения."""
    return Procedure.objects.filter(is_active=True).filter(
        Q(department__isnull=True) | Q(department=department)
    )


def search_procedures(department: Department, query: str, limit: int = 20) -> list[Procedure]:
    """Поиск процедуры для назначения по названию, подписи в карте и синонимам (FR-PAT-5).

    Сравнение — по нормализованным строкам (FR-IMP-6): «st150» находит «ST – 150».
    Справочник маленький, поэтому фильтруем в Python.
    """
    key = normalize_name(query)
    procedures = visible_procedures(department).select_related("equipment")
    if not key:
        return list(procedures[:limit])
    found = [
        procedure
        for procedure in procedures
        if any(
            key in normalize_name(text)
            for text in (procedure.name, procedure.card_label, *procedure.synonyms)
        )
    ]
    return found[:limit]
