from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.exceptions import ValidationError

from apps.common.forms import BootstrapFormMixin
from apps.org.models import Department

from . import lockout
from .models import Right, User


class LoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Неверный логин или пароль.",
        "inactive": "Учётная запись отключена. Обратитесь к администратору.",
        "locked": "Слишком много неудачных попыток. Вход заблокирован на %(minutes)s минут.",
    }

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        if username and lockout.is_locked(username):
            raise ValidationError(
                self.error_messages["locked"],
                code="locked",
                params={"minutes": settings.LOGIN_LOCKOUT_MINUTES},
            )
        try:
            cleaned = super().clean()
        except ValidationError:
            if username:
                lockout.register_failure(username)
            raise
        lockout.reset(username)
        return cleaned


class UserForm(BootstrapFormMixin, forms.ModelForm):
    """Пользователь на экране «Пользователи»: данные, отделения, права (решение 79)."""

    departments = forms.ModelMultipleChoiceField(
        queryset=Department.objects.filter(is_active=True).order_by("name"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Отделения",
        help_text="Программы этих отделений пользователь видит, а по правам — и правит.",
    )
    rights = forms.MultipleChoiceField(
        choices=Right.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Права",
    )

    class Meta:
        model = User
        fields = [
            "username",
            "last_name",
            "first_name",
            "short_name",
            "is_active",
            "is_attending_doctor",
            "rights",
        ]
        labels = {"username": "Логин", "is_active": "Учётная запись действует"}
        help_texts = {
            "username": "Для входа: латиница, цифры и знаки @ . + - _",
            "short_name": "Печатается в карте как лечащий врач, например «Иванова А.А.».",
            "is_active": "Снимите, чтобы закрыть вход. Учётные записи не удаляются: по ним "
            "подписаны программы и журнал изменений.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["departments"].initial = list(self.instance.departments.all())
        else:
            self.fields["is_active"].initial = True
            # Отделение одно (как сейчас — ОМР № 4) — сразу отмечено.
            departments = list(self.fields["departments"].queryset[:2])
            if len(departments) == 1:
                self.fields["departments"].initial = departments
            self.fields["password1"] = forms.CharField(
                label="Пароль", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
            )
            self.fields["password2"] = forms.CharField(
                label="Пароль ещё раз",
                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
            )
            for name in ("password1", "password2"):
                self.fields[name].widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_attending_doctor") and not (cleaned.get("short_name") or "").strip():
            self.add_error(
                "short_name", "Лечащему врачу нужна подпись для карты, например «Иванова А.А.»."
            )
        if "password1" in self.fields:
            first, second = cleaned.get("password1"), cleaned.get("password2")
            if first and second and first != second:
                self.add_error("password2", "Пароли не совпадают.")
            elif first:
                try:
                    password_validation.validate_password(first, self.instance)
                except ValidationError as error:
                    self.add_error("password1", error)
        return cleaned


class UserPasswordForm(BootstrapFormMixin, SetPasswordForm):
    """Новый пароль пользователю — администратор задаёт его без старого."""
