from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from . import lockout


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
