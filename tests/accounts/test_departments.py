"""Вкладка «Отделения» и снятие блокировки входа на экране «Пользователи» (TZ.md FR-ORG-3,
FR-ACC-6, решение 83)."""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts import lockout
from apps.accounts.models import LoginFailure, Preset
from apps.org import services
from apps.org.models import Department
from apps.scheduling import board_days

TUE = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def boss(make_user, department):
    return make_user("boss", (department, Preset.ADMIN))


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def form_data(name: str = "ОМР № 7", code: str = "omr7", **changes) -> dict:
    data = {
        "dep-name": name,
        "dep-code": code,
        "dep-course_3": "11",
        "dep-course_4": "15",
        "dep-course_5": "20",
        "dep-course_uk": "15",
        "dep-max_individual_per_day": "2",
    }
    return data | changes


class TestDepartments:
    def test_create_with_course_fields(self, client, boss):
        client.force_login(boss)

        html = htmx(client, reverse("users:department_add"), form_data(**{"dep-course_5": "21"}))

        new = Department.objects.get(code="omr7")
        assert "Отделение «ОМР № 7» заведено" in html and 'hx-swap-oob="true"' in html
        assert new.course_days == {"3": 11, "4": 15, "5": 21, "УК": 15}
        assert new.history.first().history_user == boss

    def test_edit_errors_in_window(self, client, boss, department):
        client.force_login(boss)
        url = reverse("users:department_edit", args=[department.pk])
        assert 'value="15"' in client.get(url).content.decode()

        html = htmx(client, url, form_data("ОМР № 4", "omr4", **{"dep-course_3": "0"}))

        assert "больше либо равно 1" in html
        department.refresh_from_db()
        assert department.course_days["3"] == 11

    def test_unused_deleted(self, client, boss):
        empty = Department.objects.create(name="Пустое", code="empty")
        client.force_login(boss)
        url = reverse("users:department_delete", args=[empty.pk])

        assert "удалится совсем" in htmx(client, url)
        htmx(client, url, {"confirm": "1"})

        assert not Department.objects.filter(pk=empty.pk).exists()

    def test_with_current_programs_not_archived(self, boss, department, make_program):
        make_program(start_date=TUE)

        with pytest.raises(services.DepartmentError, match="текущих программ: 1"):
            services.delete_department(boss, department)
        department.refresh_from_db()
        assert department.is_active

    def test_used_archived_and_restored(self, client, boss, make_program, department):
        make_program(start_date=date(2026, 9, 1))  # курс закончился
        client.force_login(boss)

        html = htmx(
            client, reverse("users:department_delete", args=[department.pk]), {"confirm": "1"}
        )

        department.refresh_from_db()
        assert not department.is_active and "в архиве" in html and "В архиве (1)" in html
        services.restore_department(boss, department)
        department.refresh_from_db()
        assert department.is_active

    def test_rights(self, client, rehab, department):
        client.force_login(rehab)
        assert client.get(reverse("users:departments")).status_code == 403
        url = reverse("users:department_delete", args=[department.pk])
        assert client.post(url, {"confirm": "1"}).status_code == 403
        with pytest.raises(PermissionDenied):
            services.save_department(rehab, department)


class TestUnlock:
    def test_locked_user_unlocked(self, client, boss, doctor, settings):
        for _ in range(settings.LOGIN_LOCKOUT_ATTEMPTS):
            lockout.register_failure("doctor")
        assert lockout.is_locked("doctor")
        client.force_login(boss)

        html = client.get(reverse("users:list")).content.decode()
        assert "вход заблокирован до" in html and "Снять блокировку" in html

        response = client.post(reverse("users:unlock", args=[doctor.pk]))

        assert response.status_code == 302
        assert not lockout.is_locked("doctor") and not LoginFailure.objects.exists()

    def test_not_locked_has_no_button(self, client, boss, doctor):
        lockout.register_failure("doctor")
        client.force_login(boss)
        html = client.get(reverse("users:list")).content.decode()
        assert "Снять блокировку" not in html

    def test_rights(self, client, rehab, doctor):
        client.force_login(rehab)
        assert client.post(reverse("users:unlock", args=[doctor.pk])).status_code == 403
