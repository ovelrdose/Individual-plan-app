from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet

from apps.accounts.access import departments_for, has_right, is_admin, require_right
from apps.accounts.models import Right, User
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

if TYPE_CHECKING:
    from apps.programs.models import Program


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


# --- Экран «Справочники»: процедуры (TZ.md, FR-CAT-7, решение 83) ---------------------------
# Группы правятся в «Групповых занятиях» (решение 82), окно тренажёра — там же. Права — здесь:
# право «Справочники», процедура отделения — только для тех, кто в нём работает.


class CatalogError(Exception):
    """Изменение справочника невозможно. Текст показывается пользователю."""


# Виды, которые ведутся на экране «Справочники»; новые — без тренажёра (нужен его режим работы).
SCREEN_KINDS = (
    ProcedureKind.INDIVIDUAL,
    ProcedureKind.CARD_ONLY,
    ProcedureKind.POOL,
    ProcedureKind.EQUIPMENT,
)
NEW_KINDS = (ProcedureKind.INDIVIDUAL, ProcedureKind.CARD_ONLY, ProcedureKind.POOL)


def can_edit_catalog(user: User) -> bool:
    return has_right(user, Right.CATALOG)


def require_catalog(user: User, procedure: Procedure | None = None) -> None:
    if not can_edit_catalog(user):
        raise PermissionDenied("Нет права править справочники.")
    if procedure is not None and procedure.department_id is not None:
        require_right(user, Right.CATALOG, procedure.department)


def catalog_procedures(user: User) -> QuerySet[Procedure]:
    """Процедуры, которые специалист видит в «Справочниках»: общие и своих отделений."""
    procedures = Procedure.objects.all()
    if not can_edit_catalog(user):
        return procedures.none()
    if not is_admin(user):
        procedures = procedures.filter(Q(department=None) | Q(department__in=departments_for(user)))
    return procedures


def filter_procedures(procedures: QuerySet[Procedure], kind: str, query: str) -> list[Procedure]:
    """Фильтр по виду и поиск по названию, подписи и синонимам (как поиск в назначении)."""
    if kind == "GROUPS":
        procedures = procedures.filter(kind__in=SESSION_KINDS, group_choice=False)
    elif kind:
        procedures = procedures.filter(kind=kind)
    else:
        procedures = procedures.exclude(kind__in=SESSION_KINDS, group_choice=False)
    items = list(procedures.select_related("department", "equipment", "section"))
    key = normalize_name(query)
    if key:
        items = [
            p
            for p in items
            if any(key in normalize_name(t) for t in (p.name, p.card_label, *p.synonyms))
        ]
    return sorted(items, key=lambda p: (p.kind, p.name.lower()))


def procedure_is_used(procedure: Procedure) -> bool:
    """Назначения (как процедура или группа бассейна), занятия, расписание группы."""
    from apps.programs.models import Prescription
    from apps.scheduling.models import Booking

    return (
        Prescription.objects.filter(Q(procedure=procedure) | Q(pool_group=procedure)).exists()
        or Booking.objects.filter(procedure=procedure).exists()
        or GroupSession.objects.filter(procedure=procedure).exists()
    )


def _check_screen_procedure(procedure: Procedure) -> None:
    procedure.name = procedure.name.strip()
    procedure.card_label = (procedure.card_label or "").strip() or procedure.name
    if procedure.kind not in SCREEN_KINDS:
        raise CatalogError("Группы заводятся и правятся в «Групповых занятиях».")
    if procedure.kind == ProcedureKind.POOL and not procedure.group_choice:
        # «Бассейн» с расписанием — это группа бассейна, у неё раздел и времена.
        raise CatalogError(
            "Здесь — только «Бассейн» без группы (группу выбирает специалист ФР). Группы "
            "бассейна заводятся в «Групповых занятиях»."
        )
    taken = Procedure.objects.filter(
        name__iexact=procedure.name, department=procedure.department_id
    ).exclude(pk=procedure.pk)
    if taken.exists():
        where = f"в отделении {procedure.department}" if procedure.department_id else "среди общих"
        raise CatalogError(f"Процедура «{procedure.name}» уже есть в справочнике {where}.")
    procedure.full_clean()


@transaction.atomic
def create_procedure(user: User, procedure: Procedure) -> Procedure:
    """Новая процедура: индивидуальное, «только строка в карте» или «Бассейн» без группы."""
    require_catalog(user, procedure)
    if procedure.kind not in NEW_KINDS:
        raise CatalogError(
            "Тренажёр заводится в «Групповых занятиях» (у него режим работы), группы — там же."
        )
    procedure.equipment = None
    procedure.section = None
    procedure.is_active = True
    _check_screen_procedure(procedure)
    procedure._history_user = user
    procedure.save()
    return procedure


@transaction.atomic
def save_procedure(user: User, procedure: Procedure) -> Procedure:
    """Правка процедуры. Вид и признаки «Бассейн без группы» / «вечернее индивидуальное»
    назначавшейся процедуры не меняются: по ним уже построены расписания и карты."""
    old = Procedure.objects.select_for_update().filter(pk=procedure.pk).first()
    if old is None:
        raise CatalogError("Процедура не найдена. Обновите страницу.")
    require_catalog(user, old)
    require_catalog(user, procedure)
    if old.is_group:
        raise CatalogError("Группы правятся в «Групповых занятиях».")
    equipment_kind = ProcedureKind.EQUIPMENT in (old.kind, procedure.kind)
    if equipment_kind and old.kind != procedure.kind:
        raise CatalogError("Вид процедуры тренажёра не меняется.")
    changed = (old.kind, old.group_choice, old.evening_individual, old.department_id) != (
        procedure.kind,
        procedure.group_choice,
        procedure.evening_individual,
        procedure.department_id,
    )
    if changed and procedure_is_used(old):
        raise CatalogError(
            f"«{old.name}» уже назначали — вид, признаки и отделение процедуры не меняются. "
            "Заведите новую."
        )
    procedure.equipment_id = old.equipment_id
    procedure.section_id = None
    procedure.is_active = old.is_active
    _check_screen_procedure(procedure)
    procedure._history_user = user
    procedure.save()
    return procedure


@dataclass(frozen=True)
class ProcedureRemoved:
    """Итог «Удалить» процедуры: совсем или в архив; у тренажёра — пересборка программ."""

    archived: bool
    rescheduled: object | None = None  # scheduling.services.Rescheduled


# Их расписание строит шахматка: молча оставить пациента без занятия нельзя (FR-CAT-7).
_GUARDED = (ProcedureKind.INDIVIDUAL, ProcedureKind.POOL)


def current_holders(procedure: Procedure, today: date | None = None) -> list["Program"]:
    # Программы и шахматка сами импортируют справочник — импорт здесь, иначе цикл.
    """Текущие программы, где процедура назначена и ещё не отменена (или выбрана группой
    бассейна)."""
    from apps.programs.models import Program, withdrawn_on
    from apps.scheduling import board_days

    today = today or board_days.today()
    return list(
        Program.objects.filter(end_date__gte=today)
        .exclude(withdrawn_on(today))
        .filter(
            Q(prescriptions__procedure=procedure) | Q(prescriptions__pool_group=procedure),
            Q(prescriptions__cancel_date__isnull=True) | Q(prescriptions__cancel_date__gt=today),
        )
        .distinct()
        .order_by("room", "full_name")
    )


def _holders_text(user: User, holders: list["Program"]) -> str:
    """«палата Фамилия» — только пациенты отделений, которые видит пользователь (FR-ACC-2);
    остальные — числом."""
    own = set(departments_for(user).values_list("pk", flat=True))
    visible = [p for p in holders if p.department_id in own]
    names = ", ".join(f"{p.room_label} {p.surname}" for p in visible[:10])
    parts = [names] if names else []
    if len(visible) > 10:
        parts.append(f"и ещё {len(visible) - 10}")
    if hidden := len(holders) - len(visible):
        parts.append(f"{'и ещё ' if names else ''}{hidden} в других отделениях")
    return " ".join(parts)


def _guarded(procedure: Procedure) -> bool:
    return procedure.kind in _GUARDED or procedure.evening_individual


@transaction.atomic
def delete_procedure(
    user: User, procedure: Procedure, *, today: date | None = None
) -> ProcedureRemoved:
    """«Удалить» процедуру (решение 83). Неиспользованная удаляется совсем. Использованная —
    в архив: пропадает из поиска и распознавания листа; «только строка в карте» остаётся в
    картах; тренажёр уходит в архив вместе с процедурой, программы пересобираются.
    Индивидуальное, «Бассейн» без группы и вечернее индивидуальное с действующими назначениями
    текущих программ в архив не уходят — их расписание строит шахматка."""
    require_catalog(user, procedure)
    if procedure.is_group:
        raise CatalogError("Группы удаляются в «Групповых занятиях».")
    if not procedure.is_active:
        raise CatalogError("Процедура уже в архиве.")
    if procedure.kind == ProcedureKind.EQUIPMENT:
        from apps.scheduling.services import ScheduleError, remove_equipment

        try:
            removed = remove_equipment(user, procedure.equipment, today=today)
        except ScheduleError as error:
            raise CatalogError(str(error)) from None
        return ProcedureRemoved(removed.archived, removed.rescheduled)
    if not procedure_is_used(procedure):
        locked = Procedure.objects.select_for_update().get(pk=procedure.pk)
        if procedure_is_used(locked):
            raise CatalogError("Процедуру только что назначили — обновите страницу.")
        locked._history_user = user
        locked.delete()
        return ProcedureRemoved(False)
    if _guarded(procedure):
        holders = current_holders(procedure, today)
        if holders:
            raise CatalogError(
                f"«{procedure.name}» назначено текущим пациентам ({len(holders)}): "
                f"{_holders_text(user, holders)}. Сначала отмените или замените назначение у "
                "них — иначе пациент молча останется без занятия."
            )
    procedure.is_active = False
    procedure._history_user = user
    procedure.save(update_fields=["is_active"])
    return ProcedureRemoved(True)


@transaction.atomic
def restore_procedure(
    user: User, procedure: Procedure, *, today: date | None = None
) -> ProcedureRemoved:
    """«Вернуть из архива»; тренажёр возвращается вместе с процедурой."""
    require_catalog(user, procedure)
    if procedure.is_group:
        # Группу возвращают там, где видно её времена и последствия (решение 82).
        raise CatalogError("Группы возвращают из архива в «Групповых занятиях».")
    if procedure.kind == ProcedureKind.EQUIPMENT:
        from apps.scheduling.services import restore_equipment_now

        return ProcedureRemoved(
            False, restore_equipment_now(user, procedure.equipment, today=today)
        )
    procedure.is_active = True
    procedure._history_user = user
    procedure.save(update_fields=["is_active"])
    return ProcedureRemoved(False)
