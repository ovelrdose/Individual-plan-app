"""Три пользователя для рабочей установки: врач, специалист ФР (инструктор) и администратор.

    .venv/bin/python manage.py create_users                     # пароли спросит
    .venv/bin/python manage.py create_users --password '...'    # один пароль всем, без вопросов
    .venv/bin/python manage.py create_users --doctor ivanova --doctor-name 'Иванова А.А.'

Отделение ОМР № 4 создаётся, если его нет. Пользователей, которые уже есть, команда не
меняет — ни пароль, ни права: повторный запуск безопасен. Работает и без DEBUG, в отличие от
``seed_dev`` (там вымышленные инструкторы и общий пароль для разработки).
"""

import getpass
import sys
from dataclasses import dataclass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.accounts.models import Membership, Preset, User
from apps.org.models import Department

DEPARTMENT_CODE = "omr4"
DEPARTMENT_NAME = "ОМР № 4"


@dataclass(frozen=True)
class Account:
    key: str  # имя параметра команды
    title: str  # как назвать в выводе
    preset: Preset | None  # None — суперпользователь
    login: str
    short_name: str


ACCOUNTS = [
    Account("doctor", "врач", Preset.DOCTOR, "doctor", "Врач"),
    Account("rehab", "специалист ФР", Preset.REHAB, "rehab", "Специалист ФР"),
    Account("admin", "администратор", None, "admin", "Администратор"),
]


class Command(BaseCommand):
    help = "Создаёт врача, специалиста ФР и администратора в ОМР № 4 (существующих не меняет)."

    def add_arguments(self, parser: CommandParser) -> None:
        for account in ACCOUNTS:
            parser.add_argument(
                f"--{account.key}",
                default=account.login,
                help=f"Логин: {account.title} (по умолчанию {account.login}).",
            )
            parser.add_argument(
                f"--{account.key}-name",
                default=account.short_name,
                help=f"Фамилия и инициалы: {account.title} (по умолчанию «{account.short_name}»). "
                "Врач печатается в карте.",
            )
        parser.add_argument(
            "--password",
            help="Один пароль для всех новых пользователей. Без него пароли спрашиваются.",
        )

    def handle(self, *args, **options) -> None:
        accounts = [
            Account(
                a.key,
                a.title,
                a.preset,
                options[a.key].strip(),
                options[f"{a.key}_name"].strip(),
            )
            for a in ACCOUNTS
        ]
        logins = [a.login for a in accounts]
        if not all(logins) or len(set(logins)) != len(logins):
            raise CommandError("Логины должны быть заданы и не совпадать.")

        existing = set(User.objects.filter(username__in=logins).values_list("username", flat=True))
        passwords = {
            a.login: self._password(a, options["password"])
            for a in accounts
            if a.login not in existing
        }

        with transaction.atomic():
            department, _ = Department.objects.get_or_create(
                code=DEPARTMENT_CODE, defaults={"name": DEPARTMENT_NAME}
            )
            for account in accounts:
                if account.login in existing:
                    self.stdout.write(f"{account.title}: {account.login} уже есть — не изменён")
                    continue
                self._create(account, passwords[account.login], department)
                self.stdout.write(
                    self.style.SUCCESS(f"{account.title}: {account.login} ({account.short_name})")
                )

    def _password(self, account: Account, common: str | None) -> str:
        user = User(username=account.login, short_name=account.short_name)
        if common is not None:
            self._validate(common, user)
            return common
        if not sys.stdin.isatty():
            raise CommandError("Нет терминала для ввода пароля — задайте --password.")
        while True:
            first = getpass.getpass(f"Пароль, {account.title} ({account.login}): ")
            second = getpass.getpass("Ещё раз: ")
            if first != second:
                self.stderr.write("Пароли не совпали.")
                continue
            try:
                self._validate(first, user)
            except CommandError as error:
                self.stderr.write(str(error))
                continue
            return first

    def _validate(self, password: str, user: User) -> None:
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("Пароль не подходит: " + " ".join(error.messages)) from error

    def _create(self, account: Account, password: str, department: Department) -> None:
        user = User(username=account.login, short_name=account.short_name)
        user.set_password(password)
        if account.preset is None:
            user.is_superuser = True
        else:
            user.apply_preset(account.preset)
        # Вход в админку (is_staff) — только у суперпользователя: его ставит сигнал (решение 83).
        user.save()
        if account.preset is not None:
            Membership.objects.create(user=user, department=department)
