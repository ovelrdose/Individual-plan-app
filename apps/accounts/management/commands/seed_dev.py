from datetime import date, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.admin_access import sync_admin_access
from apps.accounts.models import Membership, Role, User
from apps.catalog import initial_data
from apps.catalog.models import GroupSession, InstructorSlot
from apps.org.models import Department
from apps.staff.models import (
    DutyKind,
    Instructor,
    InstructorDuty,
    ShiftPattern,
    ShiftPatternKind,
)
from apps.staff.services import set_partner

DEV_USERS = [
    # логин, фамилия и инициалы, роль (None — администратор)
    ("admin", "Админов А.А.", None),
    ("doctor", "Иванова А.А.", Role.DOCTOR),
    ("rehab", "Петрова Б.Б.", Role.REHAB),
]

# Вымышленные инструкторы: (фамилия, ФИО, порядок). Пары — 2/2 со сдвигом на два дня.
DEV_INSTRUCTORS = [
    ("Соколов", "Соколов Сергей Иванович", 10),
    ("Морозова", "Морозова Мария Петровна", 20),
    ("Волков", "Волков Виктор Андреевич", 30),
    ("Лебедева", "Лебедева Людмила Олеговна", 31),
    ("Зайцев", "Зайцев Захар Ильич", 40),
    ("Орлова", "Орлова Ольга Николаевна", 50),
    ("Голубев", "Голубев Глеб Павлович", 60),
    ("Белова", "Белова Вера Сергеевна", 61),
    ("Кузнецова", "Кузнецова Ксения Игоревна", 70),
]
DEV_PAIRS = [("Волков", "Лебедева"), ("Голубев", "Белова")]

# Шаблоны смен с 01.09.2026: (фамилия, шаблон, первый рабочий день цикла).
SHIFTS_FROM = date(2026, 9, 1)
DEV_SHIFTS = [
    ("Соколов", ShiftPatternKind.FIVE_TWO, SHIFTS_FROM),
    ("Морозова", ShiftPatternKind.FIVE_TWO, SHIFTS_FROM),
    ("Волков", ShiftPatternKind.TWO_TWO, date(2026, 9, 1)),
    ("Лебедева", ShiftPatternKind.TWO_TWO, date(2026, 9, 3)),
    ("Зайцев", ShiftPatternKind.FIVE_TWO, SHIFTS_FROM),
    ("Орлова", ShiftPatternKind.FIVE_TWO, SHIFTS_FROM),
    ("Голубев", ShiftPatternKind.TWO_TWO, date(2026, 9, 2)),
    ("Белова", ShiftPatternKind.TWO_TWO, date(2026, 9, 4)),
    ("Кузнецова", ShiftPatternKind.FIVE_TWO, SHIFTS_FROM),
]

# Распорядок «как в жизни» (FR-STF-5): (фамилия, начало слота, вид, группа для GROUP_LEAD).
DEV_DUTIES = [
    ("Орлова", time(9, 10), DutyKind.GROUP_LEAD, "Эрго общая"),
    ("Орлова", time(10, 30), DutyKind.GROUP_LEAD, "I can нога"),
    ("Орлова", time(13, 0), DutyKind.BOS, None),
    ("Орлова", time(13, 40), DutyKind.BOS, None),
    ("Орлова", time(14, 20), DutyKind.BOS, None),
    ("Соколов", time(15, 0), DutyKind.METHOD_WORK, None),
    ("Соколов", time(15, 40), DutyKind.METHOD_WORK, None),
    ("Соколов", time(16, 20), DutyKind.METHOD_WORK, None),
    ("Морозова", time(15, 0), DutyKind.METHOD_WORK, None),
    ("Морозова", time(15, 40), DutyKind.METHOD_WORK, None),
    ("Морозова", time(16, 20), DutyKind.METHOD_WORK, None),
    ("Зайцев", time(11, 10), DutyKind.METHOD_WORK, None),
]


class Command(BaseCommand):
    help = (
        "Создаёт отделение ОМР № 4, тестовых пользователей и инструкторов. "
        "Только для локальной разработки."
    )

    def add_arguments(self, parser):
        parser.add_argument("--password", default="devpass123")

    @transaction.atomic
    def handle(self, *args, password: str, **options):
        if not settings.DEBUG:
            raise CommandError("Команда работает только при DEBUG=true.")

        department, _ = Department.objects.get_or_create(code="omr4", defaults={"name": "ОМР № 4"})
        for username, short_name, role in DEV_USERS:
            user, created = User.objects.get_or_create(
                username=username, defaults={"short_name": short_name}
            )
            if created:
                user.set_password(password)
                if role is None:
                    user.is_superuser = user.is_staff = True
                user.save()
            if role is not None:
                Membership.objects.get_or_create(
                    user=user, department=department, defaults={"role": role}
                )
            sync_admin_access(user)
            state = "создан" if created else "уже был"
            self.stdout.write(f"{username:8} {short_name:14} {state}")
        self.stdout.write(f"Пароль новых пользователей: {password}")

        dev_names = {name for name, _full, _order in DEV_INSTRUCTORS}
        if Instructor.objects.exclude(short_name__in=dev_names).exists():
            # Настоящие инструкторы уже загружены (load_board_instructors) — вымышленных не
            # добавляем, чтобы они не смешались в шахматке.
            self.stdout.write("Инструкторы: уже есть настоящие — вымышленные не добавлены.")
            return
        for short_name, full_name, order in DEV_INSTRUCTORS:
            Instructor.objects.get_or_create(
                short_name=short_name, defaults={"full_name": full_name, "display_order": order}
            )
        teams = []
        for pair in DEV_PAIRS:
            first, second = (Instructor.objects.get(short_name=name) for name in pair)
            if first.partner_id != second.pk:
                set_partner(first, second)
            teams.append(Instructor.objects.get(pk=first.pk).team_label)
        self.stdout.write(
            f"Инструкторы: {Instructor.objects.count()}, пары 2/2: {', '.join(teams)}"
        )
        self._seed_shifts()
        self._seed_duties()

    def _seed_shifts(self) -> None:
        # Шаблон заводим, только если у инструктора его нет: правки на экране «Смены» не затираем.
        created = 0
        for short_name, pattern, anchor in DEV_SHIFTS:
            instructor = Instructor.objects.get(short_name=short_name)
            if not instructor.shift_patterns.exists():
                ShiftPattern(
                    instructor=instructor,
                    pattern=pattern,
                    anchor_date=anchor,
                    valid_from=SHIFTS_FROM,
                ).save()
                created += 1
        self.stdout.write(f"Шаблоны смен: создано {created}")

    def _seed_duties(self) -> None:
        # Сетка слотов та же, что у load_initial_catalog: повторное создание безопасно.
        for start, end, is_evening in initial_data.SLOTS:
            InstructorSlot.objects.get_or_create(
                start=start, defaults={"end": end, "is_evening": is_evening}
            )
        created = skipped = 0
        for short_name, start, kind, group_name in DEV_DUTIES:
            instructor = Instructor.objects.get(short_name=short_name)
            slot = InstructorSlot.objects.get(start=start)
            if instructor.duties.filter(slot=slot).exists():
                continue
            session = None
            if group_name is not None:
                session = GroupSession.objects.filter(
                    procedure__name=group_name, procedure__department=None, start_time=start
                ).first()
                if session is None:
                    skipped += 1
                    continue
            InstructorDuty(
                instructor=instructor,
                slot=slot,
                kind=kind,
                group_session=session,
                valid_from=SHIFTS_FROM,
            ).save()
            created += 1
        self.stdout.write(f"Распорядок инструкторов: создано {created}")
        if skipped:
            self.stdout.write(
                f"Пропущено ведение групп ({skipped}): нет расписания групп — "
                "сначала выполните load_initial_catalog."
            )
