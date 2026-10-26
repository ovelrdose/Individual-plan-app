"""Отделения: вкладка «Отделения» экрана «Пользователи» (TZ.md FR-ORG-3, решение 83).

Право — «Пользователи и отделения». Удаление как у справочников (решение 82): неиспользованное
отделение удаляется совсем, использованное — в архив, но только без текущих программ.
"""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.access import has_right
from apps.accounts.models import Membership, Right, User
from apps.catalog.models import Procedure
from apps.programs.models import Program, withdrawn_on

from .models import Department


class DepartmentError(Exception):
    """Изменение отделения невозможно. Текст показывается пользователю."""


def can_manage_departments(user: User) -> bool:
    return has_right(user, Right.USERS)


def require_departments(user: User) -> None:
    if not can_manage_departments(user):
        raise PermissionDenied("Нет права править отделения.")


def all_departments(user: User) -> QuerySet[Department]:
    require_departments(user)
    return Department.objects.order_by("-is_active", "name")


@transaction.atomic
def save_department(user: User, department: Department) -> Department:
    """Новое отделение или правка: название, код, длительность курса по ШРМ, лимит
    индивидуальных в день (FR-ORG-1, FR-ORG-2)."""
    require_departments(user)
    department.name = department.name.strip()
    department.full_clean()
    department._history_user = user
    department.save()
    return department


def department_is_used(department: Department) -> bool:
    """Программы, сотрудники (членство), процедуры отделения."""
    return (
        Program.objects.filter(department=department).exists()
        or Membership.objects.filter(department=department).exists()
        or Procedure.objects.filter(department=department).exists()
    )


def current_programs(department: Department, today: date | None = None) -> int:
    """Текущие программы: курс не закончился и пациент не выбыл."""
    from apps.scheduling import board_days

    today = today or board_days.today()
    return (
        Program.objects.filter(department=department, end_date__gte=today)
        .exclude(withdrawn_on(today))
        .count()
    )


@dataclass(frozen=True)
class DepartmentRemoved:
    archived: bool


@transaction.atomic
def delete_department(
    user: User, department: Department, *, today: date | None = None
) -> DepartmentRemoved:
    """«Удалить» отделение. Неиспользованное удаляется совсем. Использованное уходит в архив —
    его сотрудники перестают его видеть, — но только если в нём нет текущих программ: иначе
    пациенты остались бы без отделения на экранах."""
    require_departments(user)
    department = Department.objects.select_for_update().get(pk=department.pk)
    if not department.is_active:
        raise DepartmentError("Отделение уже в архиве.")
    if not department_is_used(department):
        department._history_user = user
        department.delete()
        return DepartmentRemoved(False)
    count = current_programs(department, today)
    if count:
        raise DepartmentError(
            f"В отделении «{department.name}» текущих программ: {count}. В архив его можно "
            "убрать, когда курсы закончатся или пациенты выбудут."
        )
    department.is_active = False
    department._history_user = user
    department.save(update_fields=["is_active"])
    return DepartmentRemoved(True)


@transaction.atomic
def restore_department(user: User, department: Department) -> Department:
    require_departments(user)
    department.is_active = True
    department._history_user = user
    department.save(update_fields=["is_active"])
    return department
