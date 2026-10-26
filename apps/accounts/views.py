from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.org import services as org_services
from apps.org.forms import DepartmentForm
from apps.org.models import Department

from . import services
from .access import select_department
from .forms import LoginForm, UserForm, UserPasswordForm
from .models import PRESET_ATTENDING, PRESET_RIGHTS, RIGHT_SHORT, Preset, Right, User


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    redirect_authenticated_user = True


class PasswordChangeView(auth_views.PasswordChangeView):
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        messages.success(self.request, "Пароль изменён.")
        return super().form_valid(form)


@require_POST
def switch_department(request: HttpRequest) -> HttpResponse:
    department = select_department(request, request.POST.get("department", ""))
    messages.info(request, f"Отделение: {department.name}")
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = reverse_lazy("home")
    return redirect(next_url)


# --- Пользователи (решение 79) ----------------------------------------------------------------


def user_list(request: HttpRequest) -> HttpResponse:
    users = services.users_for(request.user)
    return render(
        request,
        "accounts/user_list.html",
        {
            "tab": "users",
            "users": [
                (
                    user,
                    services.can_edit_user(request.user, user),
                    [RIGHT_SHORT[Right(right)] for right in user.rights],
                    services.locked_until(user),
                )
                for user in users
            ],
        },
    )


@require_POST
def user_unlock(request: HttpRequest, pk: int) -> HttpResponse:
    """«Снять блокировку» входа после серии неудачных попыток (решение 83)."""
    user = get_object_or_404(User, pk=pk)
    services.unlock_user(request.user, user)
    messages.success(request, f"Блокировка входа «{user.username}» снята.")
    return redirect("users:list")


# --- Отделения (TZ.md FR-ORG-3, решение 83) -----------------------------------------------
# Таблица — только просмотр, правка — окно по шестерёнке, как в «Справочниках».


def department_list(request: HttpRequest) -> HttpResponse:
    org_services.require_departments(request.user)
    return render(
        request, "accounts/departments.html", {"tab": "departments", **_departments_context()}
    )


def _departments_context(*, selected: int | None = None, oob: bool = False) -> dict:
    items = list(Department.objects.order_by("name"))
    return {
        "departments": [d for d in items if d.is_active],
        "archived": [d for d in items if not d.is_active],
        "selected": selected,
        "oob": oob,
    }


def department_add(request: HttpRequest) -> HttpResponse:
    org_services.require_departments(request.user)
    form = DepartmentForm(request.POST or None, prefix="dep")
    if request.method == "POST" and form.is_valid():
        saved = _save_department(request, form)
        if saved is not None:
            return _department_panel(request, saved, message=f"Отделение «{saved}» заведено.")
    return _department_panel(request, None, form=form)


def department_edit(request: HttpRequest, pk: int) -> HttpResponse:
    org_services.require_departments(request.user)
    department = get_object_or_404(Department, pk=pk)
    if request.method != "POST":
        return _department_panel(request, department)
    form = DepartmentForm(request.POST, instance=department, prefix="dep")
    if form.is_valid():
        saved = _save_department(request, form)
        if saved is not None:
            return _department_panel(request, saved, message="Сохранено.")
    department.refresh_from_db()
    return _department_panel(request, department, form=form)


def _save_department(request: HttpRequest, form: DepartmentForm) -> Department | None:
    try:
        return org_services.save_department(request.user, form.instance)
    except ValidationError as error:
        for field, messages_ in error.message_dict.items():
            form.add_error(field if field in form.fields else None, messages_)
        return None


@require_POST
def department_delete(request: HttpRequest, pk: int) -> HttpResponse:
    org_services.require_departments(request.user)
    department = get_object_or_404(Department, pk=pk)
    if not request.POST.get("confirm"):
        used = org_services.department_is_used(department)
        current = org_services.current_programs(department) if used else 0
        details = [f"Текущих программ: {current} — в архив его не убрать."] if current else []
        confirm = {
            "used": used,
            "programs": None,
            "details": details,
            # С текущими программами в архив не убрать — кнопка заведомо дала бы ошибку.
            "blocked": bool(current),
            "url": reverse("users:department_delete", args=[department.pk]),
        }
        return _department_panel(request, department, confirm=confirm)
    try:
        removed = org_services.delete_department(request.user, department)
    except org_services.DepartmentError as error:
        return _department_panel(request, department, error=str(error))
    if not removed.archived:
        return _department_panel(
            request, None, message=f"Отделение «{department}» удалено.", closed=True
        )
    department.refresh_from_db()
    return _department_panel(
        request,
        department,
        message=f"Отделение «{department}» в архиве: его сотрудники больше его не видят.",
    )


@require_POST
def department_restore(request: HttpRequest, pk: int) -> HttpResponse:
    org_services.require_departments(request.user)
    department = get_object_or_404(Department, pk=pk)
    org_services.restore_department(request.user, department)
    department.refresh_from_db()
    return _department_panel(request, department, message="Отделение снова действует.")


def _department_panel(
    request: HttpRequest,
    department: Department | None,
    *,
    form: DepartmentForm | None = None,
    message: str = "",
    error: str = "",
    confirm: dict | None = None,
    closed: bool = False,
) -> HttpResponse:
    if department is not None and form is None:
        form = DepartmentForm(instance=department, prefix="dep")
    panel = render_to_string(
        "accounts/_department_panel.html",
        {
            "department": department,
            "form": None if closed else form,
            "message": message,
            "error": error,
            "confirm": confirm,
        },
        request=request,
    )
    table = render_to_string(
        "accounts/_departments.html",
        _departments_context(selected=department.pk if department else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + table)


def user_create(request: HttpRequest) -> HttpResponse:
    if not services.can_manage_users(request.user):
        raise PermissionDenied("Нет права создавать пользователей.")
    return _user_form(request, User(), "Новый пользователь")


def user_edit(request: HttpRequest, pk: int) -> HttpResponse:
    user = get_object_or_404(User, pk=pk)
    services.require_edit_user(request.user, user)
    return _user_form(request, user, str(user))


def _user_form(request: HttpRequest, user: User, title: str) -> HttpResponse:
    form = UserForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_user(
                request.user,
                form.save(commit=False),
                departments=form.cleaned_data["departments"],
                password=form.cleaned_data.get("password1"),
            )
        except services.UsersError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, f"Пользователь «{user}» сохранён.")
            return redirect("users:list")
    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form,
            "title": title,
            "editing": user if user.pk else None,
            "presets": [
                (preset.label, [r.value for r in PRESET_RIGHTS[preset]], PRESET_ATTENDING[preset])
                for preset in Preset
            ],
        },
    )


def user_password(request: HttpRequest, pk: int) -> HttpResponse:
    user = get_object_or_404(User, pk=pk)
    services.require_edit_user(request.user, user)
    form = UserPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.set_password(request.user, user, form.cleaned_data["new_password1"])
        messages.success(request, f"Пароль пользователя «{user}» изменён.")
        return redirect("users:list")
    return render(request, "accounts/user_password.html", {"form": form, "editing": user})
