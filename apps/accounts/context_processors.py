from django.http import HttpRequest

from .access import current_department, departments_for, has_right, is_admin
from .models import Right


def department(request: HttpRequest) -> dict:
    """Текущее отделение и права пользователя — для шапки и шаблонов."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {
        "current_department": current_department(request),
        "user_departments": list(departments_for(user)),
        "user_is_admin": is_admin(user),
        "can_edit_group_schedule": has_right(user, Right.GROUP_SCHEDULE),
        "can_manage_staff": has_right(user, Right.STAFF),
        "can_manage_users": has_right(user, Right.USERS),
    }
