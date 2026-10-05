from datetime import timedelta

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import LoginFailure
from tests.conftest import PASSWORD

LOGIN_URL = reverse("login")


def login(client, username, password=PASSWORD):
    return client.post(LOGIN_URL, {"username": username, "password": password})


def is_logged_in(client):
    return "_auth_user_id" in client.session


@pytest.mark.django_db
def test_pages_require_login(client):
    response = client.get(reverse("home"))

    assert response.status_code == 302
    assert response.url.startswith(LOGIN_URL)


@pytest.mark.django_db
def test_login_page_is_open(client):
    response = client.get(LOGIN_URL)

    assert response.status_code == 200
    assert "Вход в систему" in response.content.decode()


def test_login_and_logout(client, doctor):
    response = login(client, "doctor")

    assert response.status_code == 302
    assert response.url == reverse("home")
    assert is_logged_in(client)

    client.post(reverse("logout"))
    assert not is_logged_in(client)


def test_wrong_password_message(client, doctor):
    response = login(client, "doctor", "wrong-password")

    assert not is_logged_in(client)
    assert "Неверный логин или пароль." in response.content.decode()


def test_lockout_after_too_many_failures(client, doctor):
    for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
        login(client, "doctor", "wrong-password")

    response = login(client, "doctor")

    assert not is_logged_in(client)
    assert "Вход заблокирован на 15 минут" in response.content.decode()


def test_lockout_ignores_username_case(client, doctor):
    for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
        login(client, "DOCTOR ", "wrong-password")

    login(client, "doctor")

    assert not is_logged_in(client)


def test_lockout_expires(client, doctor):
    for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
        login(client, "doctor", "wrong-password")
    expired = timezone.now() - timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES + 1)
    LoginFailure.objects.update(created_at=expired)

    login(client, "doctor")

    assert is_logged_in(client)


def test_success_resets_failures(client, doctor):
    for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS - 1):
        login(client, "doctor", "wrong-password")
    login(client, "doctor")

    assert not LoginFailure.objects.filter(username="doctor").exists()


def test_lockout_is_per_user(client, doctor, rehab):
    for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
        login(client, "doctor", "wrong-password")

    login(client, "rehab")

    assert is_logged_in(client)


def test_inactive_user_cannot_login(client, doctor):
    doctor.is_active = False
    doctor.save()

    login(client, "doctor")

    assert not is_logged_in(client)


def test_session_expires_after_idle_hours():
    assert settings.SESSION_COOKIE_AGE == 8 * 3600
    assert settings.SESSION_SAVE_EVERY_REQUEST
