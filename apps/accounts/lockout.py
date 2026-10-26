"""Блокировка входа после серии неудачных попыток (TZ.md, NFR-6).

Считаем по логину, а не по IP: в сети клиники за одним адресом может работать много людей.
Записи хранятся в БД, чтобы блокировка работала одинаково во всех процессах gunicorn.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import LoginFailure


def _normalize(username: str) -> str:
    return username.strip().lower()


def _window_start():
    return timezone.now() - timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)


def is_locked(username: str) -> bool:
    recent = LoginFailure.objects.filter(
        username=_normalize(username), created_at__gte=_window_start()
    )
    return recent.count() >= settings.LOGIN_LOCKOUT_ATTEMPTS


def register_failure(username: str) -> None:
    LoginFailure.objects.filter(created_at__lt=_window_start()).delete()
    LoginFailure.objects.create(username=_normalize(username))


def reset(username: str) -> None:
    LoginFailure.objects.filter(username=_normalize(username)).delete()


def locked_until(username: str) -> datetime | None:
    """Когда блокировка закончится: самая ранняя из последних N неудач плюс окно; None — вход
    не заблокирован."""
    recent = list(
        LoginFailure.objects.filter(username=_normalize(username), created_at__gte=_window_start())
        .order_by("-created_at")
        .values_list("created_at", flat=True)[: settings.LOGIN_LOCKOUT_ATTEMPTS]
    )
    if len(recent) < settings.LOGIN_LOCKOUT_ATTEMPTS:
        return None
    return recent[-1] + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
