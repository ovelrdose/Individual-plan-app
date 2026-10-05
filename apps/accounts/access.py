"""Проверка прав: роль пользователя в отделении (TZ.md §3, FR-ACC-2, FR-ACC-4).

Сервисы вызывают эти функции сами — права не должны зависеть только от шаблонов.
"""

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import Http404, HttpRequest

from apps.org.models import Department

from .models import Membership, Role, User

SESSION_DEPARTMENT_KEY = "department_id"


def is_admin(user: User) -> bool:
    return user.is_authenticated and user.is_active and user.is_superuser


def departments_for(user: User) -> QuerySet[Department]:
    """Действующие отделения, которые видит пользователь."""
    departments = Department.objects.filter(is_active=True)
    if is_admin(user):
        return departments
    if not user.is_authenticated or not user.is_active:
        return departments.none()
    return departments.filter(memberships__user=user)


def role_in(user: User, department: Department) -> Role | None:
    membership = Membership.objects.filter(user=user, department=department).first()
    return Role(membership.role) if membership else None


def has_role(user: User, department: Department, *roles: Role) -> bool:
    """Администратор может всё; остальные — если их роль в отделении среди перечисленных."""
    if is_admin(user):
        return True
    if not user.is_authenticated or not user.is_active or not department.is_active:
        return False
    return role_in(user, department) in roles


def has_role_anywhere(user: User, *roles: Role) -> bool:
    """Для ресурсов центра (расписание групп, тренажёры): роль в любом действующем отделении."""
    if is_admin(user):
        return True
    if not user.is_authenticated or not user.is_active:
        return False
    return Membership.objects.filter(user=user, role__in=roles, department__is_active=True).exists()


def require_role(user: User, department: Department, *roles: Role) -> None:
    if not has_role(user, department, *roles):
        raise PermissionDenied("Недостаточно прав для этого действия в отделении.")


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
