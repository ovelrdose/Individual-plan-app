from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import PRESET_RIGHTS, Membership, Preset, User
from apps.org.models import Department

PASSWORD = "Reabilitaciya-2026"


def run(*args: str) -> str:
    out = StringIO()
    call_command("create_users", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestCreateUsers:
    def test_creates_three_users_in_omr4(self):
        run("--password", PASSWORD)

        department = Department.objects.get(code="omr4")
        doctor, rehab, admin = (
            User.objects.get(username=name) for name in ("doctor", "rehab", "admin")
        )
        assert Membership.objects.filter(user=doctor, department=department).exists()
        assert Membership.objects.filter(user=rehab, department=department).exists()
        assert doctor.is_attending_doctor and not rehab.is_attending_doctor
        assert rehab.rights == [right.value for right in PRESET_RIGHTS[Preset.REHAB]]
        assert not admin.memberships.exists() and admin.is_superuser and admin.is_staff
        assert all(u.check_password(PASSWORD) for u in (doctor, rehab, admin))

    def test_admin_access_follows_rights(self):
        run("--password", PASSWORD)

        rehab = User.objects.get(username="rehab")
        doctor = User.objects.get(username="doctor")
        assert rehab.is_staff and not rehab.groups.exists()
        assert not doctor.is_staff

    def test_custom_logins_and_names(self):
        run(
            "--password",
            PASSWORD,
            "--doctor",
            "ivanova",
            "--doctor-name",
            "Иванова А.А.",
            "--rehab",
            "petrova",
        )

        assert User.objects.get(username="ivanova").short_name == "Иванова А.А."
        assert User.objects.get(username="petrova").short_name == "Специалист ФР"

    def test_existing_users_untouched(self):
        run("--password", PASSWORD)
        doctor = User.objects.get(username="doctor")
        doctor.set_password("Drugoi-parol-2026")
        doctor.save()

        output = run("--password", "Tretiy-parol-2026")

        assert "уже есть" in output
        assert User.objects.get(username="doctor").check_password("Drugoi-parol-2026")
        assert User.objects.count() == 3 and Membership.objects.count() == 2

    def test_keeps_existing_department(self):
        Department.objects.create(code="omr4", name="ОМР № 4")

        run("--password", PASSWORD)

        assert Department.objects.count() == 1

    def test_weak_password_rejected(self):
        with pytest.raises(CommandError, match="Пароль не подходит"):
            run("--password", "12345")

        assert not User.objects.exists()

    def test_same_logins_rejected(self):
        with pytest.raises(CommandError, match="не совпадать"):
            run("--password", PASSWORD, "--rehab", "doctor")

    def test_no_terminal_needs_password(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(CommandError, match="--password"):
            run()

    def test_asks_password_until_valid(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        answers = iter(["short", "short", PASSWORD, "другой", PASSWORD, PASSWORD] + [PASSWORD] * 4)
        monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))

        run()

        assert User.objects.get(username="doctor").check_password(PASSWORD)
