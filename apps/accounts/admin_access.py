"""Доступ к админке Django (TZ.md §4: справочники ведутся в админке).

Правило одно: в админку входят администратор и специалисты ФР. Специалисту ФР выдаётся
группа с правами на справочники групп, тренажёров и инструкторов — без удаления
(вместо удаления у записей есть флаг «действует»). Врачу админка не нужна.
"""

from django.contrib.auth.models import Group, Permission
from django.db.models import Q

from .models import Role, User

REHAB_GROUP = "Специалист ФР"
REHAB_PERMISSIONS = {
    ("catalog", "procedure"): ("add", "change", "view"),
    ("catalog", "groupsession"): ("add", "change", "view"),
    ("catalog", "equipment"): ("add", "change", "view"),
    ("catalog", "instructorslot"): ("view",),
    ("staff", "instructor"): ("add", "change", "view"),
}


def rehab_group() -> Group:
    group, _ = Group.objects.get_or_create(name=REHAB_GROUP)
    condition = Q(pk__in=[])
    for (app_label, model), actions in REHAB_PERMISSIONS.items():
        codenames = [f"{action}_{model}" for action in actions]
        condition |= Q(content_type__app_label=app_label, codename__in=codenames)
    group.permissions.set(Permission.objects.filter(condition))
    return group


def sync_admin_access(user: User) -> None:
    """Приводит is_staff и группу пользователя в соответствие с его ролями."""
    is_rehab = user.memberships.filter(role=Role.REHAB, department__is_active=True).exists()
    group = rehab_group()
    if is_rehab:
        user.groups.add(group)
    else:
        user.groups.remove(group)

    should_be_staff = user.is_superuser or is_rehab
    if user.is_staff != should_be_staff:
        user.is_staff = should_be_staff
        user.save(update_fields=["is_staff"])
