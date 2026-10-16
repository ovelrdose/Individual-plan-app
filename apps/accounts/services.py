"""Пользователи: создание, права, отделения, пароль (TZ.md §3, решение 79).

Экран «Пользователи» — для тех, у кого есть право «Пользователи и отделения». Учётные записи не
удаляются, а отключаются: по ним подписаны врачи в программах и записи журнала изменений.
"""

from collections.abc import Iterable

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet

from apps.org.models import Department

from .access import has_right
from .models import Membership, Right, User


class UsersError(Exception):
    """Изменение пользователя невозможно. Текст показывается администратору."""


def can_manage_users(user: User) -> bool:
    return has_right(user, Right.USERS)


def can_edit_user(actor: User, target: User) -> bool:
    """Суперпользователя (технического администратора сервера) правит только суперпользователь."""
    return can_manage_users(actor) and (actor.is_superuser or not target.is_superuser)


def require_edit_user(actor: User, target: User) -> None:
    if not can_edit_user(actor, target):
        raise PermissionDenied("Нет права менять этого пользователя.")


def users_for(actor: User) -> QuerySet[User]:
    if not can_manage_users(actor):
        raise PermissionDenied("Нет права на список пользователей.")
    return User.objects.prefetch_related("departments").order_by(
        "-is_active", "last_name", "first_name", "username"
    )


@transaction.atomic
def save_user(
    actor: User,
    user: User,
    *,
    departments: Iterable[Department],
    password: str | None = None,
) -> User:
    """Сохраняет пользователя с правами и отделениями; ``password`` — для нового."""
    require_edit_user(actor, user)
    if user.pk == actor.pk and not actor.is_superuser:
        # Иначе можно остаться без единого администратора и без доступа к этому экрану.
        if Right.USERS not in user.rights:
            raise UsersError("Нельзя снять с себя право «Пользователи и отделения».")
        if not user.is_active:
            raise UsersError("Нельзя отключить свою учётную запись.")
    # Порядок прав — как в списке, без повторов: так их проще сравнивать и показывать.
    user.rights = [right.value for right in Right if right in user.rights]
    if password:
        user.set_password(password)
    user.save()
    wanted = {department.pk for department in departments}
    Membership.objects.filter(user=user).exclude(department__in=wanted).delete()
    current = set(Membership.objects.filter(user=user).values_list("department_id", flat=True))
    Membership.objects.bulk_create(
        Membership(user=user, department_id=pk) for pk in sorted(wanted - current)
    )
    return user


def set_password(actor: User, user: User, password: str) -> None:
    require_edit_user(actor, user)
    user.set_password(password)
    user.save(update_fields=["password"])
