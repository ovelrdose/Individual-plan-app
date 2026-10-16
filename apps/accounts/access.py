"""Проверка прав (TZ.md §3, FR-ACC-2, FR-ACC-4, решение 79).

У пользователя — набор прав (`User.rights`) и отделения, в которых он работает. Права общие
для всех его отделений; программы отделения видит каждый, кто в нём работает. Суперпользователь
и право «Пользователи и отделения» — сотрудник центра: видит все отделения.

Сервисы вызывают эти функции сами — права не должны зависеть только от шаблонов.
"""

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import Http404, HttpRequest

from apps.org.models import Department

from .models import Right, User

SESSION_DEPARTMENT_KEY = "department_id"


def _active(user: User) -> bool:
    return user.is_authenticated and user.is_active


def is_admin(user: User) -> bool:
    """Сотрудник центра: все отделения, пользователи и отделения."""
    return _active(user) and (user.is_superuser or Right.USERS in user.rights)


def has_right(user: User, right: Right, department: Department | None = None) -> bool:
    """Право есть, и — если указано отделение — пользователь в нём работает."""
    if not _active(user):
        return False
    if not (user.is_superuser or right in user.rights):
        return False
    if department is None:
        return True
    return department.is_active and departments_for(user).filter(pk=department.pk).exists()


def require_right(user: User, right: Right, department: Department | None = None) -> None:
    if not has_right(user, right, department):
        raise PermissionDenied("Недостаточно прав для этого действия.")


def departments_for(user: User) -> QuerySet[Department]:
    """Действующие отделения, которые видит пользователь."""
    departments = Department.objects.filter(is_active=True)
    if is_admin(user):
        return departments
    if not _active(user):
        return departments.none()
    return departments.filter(memberships__user=user)


def departments_with(user: User, right: Right) -> QuerySet[Department]:
    """Отделения, где пользователь может сделать то, что даёт право."""
    departments = departments_for(user)
    return departments if has_right(user, right) else departments.none()


def is_attending_doctor(user: User, department: Department) -> bool:
    """Пользователь — лечащий врач в этом отделении: им он и становится в своих программах."""
    return (
        _active(user)
        and user.is_attending_doctor
        and user.memberships.filter(department=department).exists()
    )


def doctors_for(department: Department) -> QuerySet[User]:
    """Кого можно выбрать лечащим врачом программы отделения."""
    return User.objects.filter(
        is_active=True, is_attending_doctor=True, memberships__department=department
    ).order_by("short_name", "last_name", "username")


def get_department_or_404(user: User, department_id: int | str) -> Department:
    """Чужое отделение неотличимо от несуществующего (FR-ACC-2)."""
    try:
        return departments_for(user).get(pk=department_id)
    except (Department.DoesNotExist, ValueError) as error:
        raise Http404("Отделение не найдено.") from error


def current_department(request: HttpRequest) -> Department | None:
    """Отделение, выбранное в шапке. Если выбора нет или он устарел — первое доступное."""
    available = departments_for(request.user)
    selected_id = request.session.get(SESSION_DEPARTMENT_KEY)
    department = available.filter(pk=selected_id).first() if selected_id else None
    if department is None:
        department = available.first()
        if department is not None:
            request.session[SESSION_DEPARTMENT_KEY] = department.pk
    return department


def select_department(request: HttpRequest, department_id: int | str) -> Department:
    department = get_department_or_404(request.user, department_id)
    request.session[SESSION_DEPARTMENT_KEY] = department.pk
    return department
