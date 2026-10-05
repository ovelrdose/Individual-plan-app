from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .access import select_department
from .forms import LoginForm


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
