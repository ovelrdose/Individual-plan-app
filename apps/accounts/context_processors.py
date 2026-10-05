from django.http import HttpRequest

from .access import current_department, departments_for, has_role_anywhere, is_admin, role_in
from .models import Role


def department(request: HttpRequest) -> dict:
    """Текущее отделение и роль пользователя в нём — для шапки и шаблонов."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    selected = current_department(request)
    role = role_in(user, selected) if selected else None
    return {
        "current_department": selected,
        "user_departments": list(departments_for(user)),
        "current_role": role,
        "user_is_admin": is_admin(user),
        "can_edit_group_schedule": has_role_anywhere(user, Role.REHAB),
        # Смены и распорядок инструкторов — те же специалист ФР и администратор (решение 46).
        "can_manage_staff": has_role_anywhere(user, Role.REHAB),
    }
