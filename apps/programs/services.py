"""Индивидуальные программы и назначения (TZ.md §5). Все изменения — только через эти функции:
здесь проверяются роль, отделение и правила курса (FR-ACC-4)."""

from datetime import date
from enum import StrEnum

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q, QuerySet
from django.http import Http404

from apps.accounts.access import departments_for, has_role, is_admin, require_role
from apps.accounts.models import Role, User
from apps.catalog.models import SCHEDULED_KINDS
from apps.org.models import Department
from apps.scheduling.services import replan

from .domain import fold, room_sort_key
from .models import Prescription, Program


class ProgramsError(Exception):
    """Действие невозможно по правилам программы. Текст показывается пользователю."""


class Period(StrEnum):
    CURRENT = "current"
    FINISHED = "finished"
    ALL = "all"


# --- Чтение ---------------------------------------------------------------------------


def programs_for(
    user: User,
    department: Department,
    *,
    period: Period = Period.CURRENT,
    query: str = "",
    today: date | None = None,
) -> list[Program]:
    """Список программ отделения (FR-PRG-4) по порядку палат.

    У каждой программы есть `unrecognized_count` — число нераспознанных назначений.
    Поиск — по ФИО и ИБ без учёта регистра и «ё»: в отделении десятки программ,
    поэтому ищем и сортируем в Python — так проще и честнее, чем в SQL.
    """
    today = today or date.today()
    programs = (
        Program.objects.filter(department__in=departments_for(user), department=department)
        .select_related("attending_doctor")
        .annotate(
            unrecognized_count=Count(
                "prescriptions", filter=Q(prescriptions__procedure=None), distinct=True
            ),
            booking_count=Count("bookings", distinct=True),
            schedulable_count=Count(
                "prescriptions",
                filter=Q(prescriptions__procedure__kind__in=SCHEDULED_KINDS),
                distinct=True,
            ),
        )
    )
    if period == Period.CURRENT:
        programs = programs.filter(end_date__gte=today)
    elif period == Period.FINISHED:
        programs = programs.filter(end_date__lt=today)
    key = fold(query.strip())
    found = [
        p for p in programs if not key or key in fold(p.full_name) or key in fold(p.history_number)
    ]
    return sorted(found, key=lambda p: (room_sort_key(p.room), p.full_name))


def get_program_or_404(user: User, pk: int) -> Program:
    """Программа чужого отделения неотличима от несуществующей (FR-ACC-2)."""
    program = (
        Program.objects.filter(pk=pk, department__in=departments_for(user))
        .select_related("department", "attending_doctor")
        .first()
    )
    if program is None:
        raise Http404("Программа не найдена.")
    return program


def doctors_for(department: Department) -> QuerySet[User]:
    return User.objects.filter(
        is_active=True, memberships__department=department, memberships__role=Role.DOCTOR
    ).order_by("short_name", "last_name")


def can_edit(user: User, program: Program) -> bool:
    """Данные программы и назначения правит врач отделения и администратор (§3)."""
    return has_role(user, program.department, Role.DOCTOR)


def can_create(user: User, department: Department) -> bool:
    return has_role(user, department, Role.DOCTOR)


def review_items(program: Program) -> list[str]:
    """Что проверить на странице программы (FR-PRG-6): сохранённые предупреждения импорта
    и то, что видно по текущим данным (нераспознанные строки)."""
    items = list(program.import_warnings)
    unrecognized = program.prescriptions.filter(procedure=None).count()
    if unrecognized:
        items.append(
            f"Не распознано назначений: {unrecognized} — выберите для них процедуру "
            "(строки подсвечены)."
        )
    return items


# --- Программа ------------------------------------------------------------------------


@transaction.atomic
def save_program(user: User, program: Program, *, end_date_changed: bool) -> Program:
    """Создаёт или сохраняет программу.

    Дата окончания (FR-PRG-2): если врач её не трогал и она не задавалась вручную, она
    пересчитывается по ШРМ и дате начала. Если врач ввёл дату, отличную от расчётной, —
    это продление или досрочное окончание, дальше она не пересчитывается.
    """
    require_role(user, program.department, Role.DOCTOR)
    if program.pk:
        _lock(program)
    if not doctors_for(program.department).filter(pk=program.attending_doctor_id).exists():
        raise ProgramsError("Лечащий врач должен быть врачом этого отделения.")

    planned = program.planned_end_date()
    if end_date_changed and program.end_date is not None:
        program.end_date_manual = program.end_date != planned
    elif program.end_date is None or not program.end_date_manual:
        program.end_date = planned
        program.end_date_manual = False

    program.full_clean()
    existing = program.pk is not None
    if existing:
        _check_prescriptions_fit(program)
    _save(program, user)
    if existing:
        # Курс сдвинулся или изменился — расписание следует за ним (FR-SCH-12).
        replan(program)
    return program


@transaction.atomic
def dismiss_warnings(user: User, program: Program) -> None:
    """Врач проверил предупреждения импорта и закрыл блок (FR-PRG-6)."""
    program = _lock(program)
    require_role(user, program.department, Role.DOCTOR)
    program.import_warnings = []
    _save(program, user, update_fields=["import_warnings", "updated_at"])


@transaction.atomic
def delete_program(user: User, program: Program) -> None:
    """Удаление программы целиком — только администратор (решение владельца 04.10).
    Уходят назначения, расписание и журнал выгрузок; запись об удалении остаётся в истории."""
    if not is_admin(user):
        raise PermissionDenied("Удалять программы может только администратор.")
    program = _lock(program)
    program._history_user = user
    program.delete()


# --- Назначения -----------------------------------------------------------------------


EDITABLE_FIELDS = ["procedure", "duration_min", "per_day", "start_date", "cancel_date", "in_card"]


@transaction.atomic
def add_prescription(user: User, prescription: Prescription) -> Prescription:
    """Новое назначение встаёт в конец списка — порядок строк в карте = порядок назначений."""
    require_editable(user, _lock(prescription.program))
    last = prescription.program.prescriptions.aggregate(last=Max("card_order"))["last"]
    prescription.card_order = (last or 0) + 1
    prescription.full_clean()
    _save(prescription, user)
    replan(prescription.program)
    return prescription


@transaction.atomic
def update_prescription(user: User, prescription: Prescription) -> Prescription:
    """Сохраняет только редактируемые поля: порядок строк меняет move_prescription."""
    program = _lock(prescription.program)
    require_editable(user, program)
    _require_exists(program, prescription)
    prescription.full_clean()
    _save(prescription, user, update_fields=EDITABLE_FIELDS)
    replan(program)
    return prescription


@transaction.atomic
def delete_prescription(user: User, prescription: Prescription) -> None:
    program = _lock(prescription.program)
    require_editable(user, program)
    _require_exists(program, prescription)
    prescription._history_user = user
    prescription.delete()
    replan(program)


@transaction.atomic
def move_prescription(user: User, prescription: Prescription, step: int) -> None:
    """Сдвигает строку на позицию вверх (step=-1) или вниз (step=1) в порядке карты."""
    program = _lock(prescription.program)
    require_editable(user, program)
    _require_exists(program, prescription)
    items = list(program.prescriptions.all())
    index = next(i for i, item in enumerate(items) if item.pk == prescription.pk)
    target = index + step
    if not 0 <= target < len(items):
        return
    items[index], items[target] = items[target], items[index]
    # Перенумеровываем подряд: так порядок однозначен даже после удалений строк.
    for order, item in enumerate(items, start=1):
        if item.card_order != order:
            item.card_order = order
            _save(item, user, update_fields=["card_order"])


def require_editable(user: User, program: Program) -> None:
    require_role(user, program.department, Role.DOCTOR)


# --- Внутреннее -----------------------------------------------------------------------


def _save(instance: Program | Prescription, user: User, **kwargs) -> None:
    """Сохраняет с автором в журнале изменений (NFR-5) — и вне веб-запроса тоже."""
    instance._history_user = user
    instance.save(**kwargs)


def _lock(program: Program) -> Program:
    """Блокируем программу: правки одной программы идут по очереди."""
    return (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=program.pk)
    )


def _require_exists(program: Program, prescription: Prescription) -> None:
    # Строку могли удалить в соседней вкладке, пока эта форма была открыта.
    if not program.prescriptions.filter(pk=prescription.pk).exists():
        raise ProgramsError("Назначение уже удалено. Обновите страницу.")


def _check_prescriptions_fit(program: Program) -> None:
    """Новые даты курса должны вмещать даты назначений — иначе строки стали бы
    неисправимыми (любая их правка упиралась бы в «вне курса»)."""
    problems = []
    for item in program.prescriptions.select_related("procedure"):
        item.program = program  # проверяем по новым датам курса
        try:
            item.clean()
        except ValidationError as error:
            if {"start_date", "cancel_date"} & set(error.message_dict):
                problems.append(item.label)
    if problems:
        raise ProgramsError(
            "Даты назначений не попадают в новый курс: "
            + ", ".join(problems)
            + ". Сначала поправьте или удалите эти назначения."
        )
