from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

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
            "users": [
                (
                    user,
                    services.can_edit_user(request.user, user),
                    [RIGHT_SHORT[Right(right)] for right in user.rights],
                )
                for user in users
            ]
        },
    )


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
