"""Доступ к админке Django (TZ.md §4: справочники ведутся в админке; решение 79).

Права Django и группы не используются: раздел админки сам проверяет право пользователя
(`RightAdminMixin`). В админку входят суперпользователь и те, у кого есть «Справочники» или
«Пользователи и отделения» — флаг is_staff выставляет сигнал при сохранении пользователя
(accounts/signals.py), вручную его не ставят.
Удаление в админке — только у суперпользователя: у записей справочников есть флаг «действует».
"""

from django.http import HttpRequest

from .access import has_right
from .models import Right, User

ADMIN_RIGHTS = (Right.CATALOG, Right.USERS)


def needs_admin(user: User) -> bool:
    """Нужен ли пользователю вход в админку."""
    return user.is_superuser or any(right in user.rights for right in ADMIN_RIGHTS)


class RightAdminMixin:
    """Раздел админки доступен по праву ``right``; ``read_only`` — только просмотр."""

    right: Right = Right.CATALOG
    read_only = False

    def _allowed(self, request: HttpRequest) -> bool:
        return request.user.is_superuser or has_right(request.user, self.right)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self._allowed(request)

    def has_view_permission(self, request: HttpRequest, obj=None) -> bool:
        return self._allowed(request)

    def has_add_permission(self, request: HttpRequest, *args) -> bool:
        return request.user.is_superuser or (self._allowed(request) and not self.read_only)

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return request.user.is_superuser or (self._allowed(request) and not self.read_only)

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return request.user.is_superuser
