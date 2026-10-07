from datetime import date, time

import pytest

from apps.accounts.models import Membership, Role, User
from apps.catalog.models import InstructorSlot
from apps.org.models import Department
from apps.staff.models import Instructor, ShiftPattern
from apps.staff.services import set_partner

PASSWORD = "test-pass-123"


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    """Настоящий хешер намеренно медленный — в тестах он только тормозит."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def department(db):
    return Department.objects.create(name="ОМР № 4", code="omr4")


@pytest.fixture
def other_department(db):
    return Department.objects.create(name="ОМР № 1", code="omr1")


@pytest.fixture
def make_user(db):
    """make_user("doctor", (department, Role.DOCTOR), ...) — пользователь с членством."""

    def make(username: str, *memberships: tuple[Department, Role], **fields) -> User:
        user = User.objects.create_user(username=username, password=PASSWORD, **fields)
        for dept, role in memberships:
            Membership.objects.create(user=user, department=dept, role=role)
        return user

    return make


@pytest.fixture
def admin_user(make_user):
    return make_user("admin", is_superuser=True, is_staff=True)


@pytest.fixture
def doctor(make_user, department):
    return make_user("doctor", (department, Role.DOCTOR), short_name="Иванова А.А.")


@pytest.fixture
def rehab(make_user, department):
    return make_user("rehab", (department, Role.REHAB), short_name="Петрова Б.Б.")


@pytest.fixture
def procedures(db):
    """Небольшой справочник: группа ЛФК, индивидуальное, тренажёр, строка в карте."""
    from apps.catalog.models import Equipment, Procedure, ProcedureKind

    st = Equipment.objects.create(name="st-150")
    items = {
        "group": Procedure(
            name="Эрго общая",
            card_label="Группа Эрго общая",
            kind=ProcedureKind.LFK_GROUP,
            default_duration_min=30,
        ),
        "individual": Procedure(
            name="Индивидуальное занятие",
            card_label="Инд.занятие",
            kind=ProcedureKind.INDIVIDUAL,
            default_duration_min=30,
        ),
        "equipment": Procedure(
            name="st-150",
            card_label="st-150",
            kind=ProcedureKind.EQUIPMENT,
            equipment=st,
            synonyms=["ST – 150"],
            default_duration_min=15,
        ),
        "card_only": Procedure(
            name="Эрготерапевт",
            card_label="Эрготерапевт",
            kind=ProcedureKind.CARD_ONLY,
            synonyms=["Консультация эрготерапевта"],
        ),
    }
    for item in items.values():
        item.save()
    return items


@pytest.fixture
def make_program(doctor, department):
    """make_program(shrm=4, ...) — программа через сервис, как её заводит врач."""
    from datetime import date

    from apps.programs.models import Program
    from apps.programs.services import save_program

    def make(**fields):
        values = {
            "department": department,
            "full_name": "Тестов Тест Тестович",
            "sex": "М",
            "age": 52,
            "history_number": "100",
            "room": "9",
            "shrm": 4,
            "diagnosis": "I69.4",
            "attending_doctor": doctor,
            "start_date": date(2026, 10, 5),
        } | fields
        return save_program(doctor, Program(**values), end_date_changed=False)

    return make


@pytest.fixture
def full_catalog(department):
    """Справочник как в жизни: load_initial_catalog из расписаний групп заказчика."""
    from pathlib import Path

    from django.conf import settings

    from apps.catalog.services import load_initial_catalog

    docs = Path(settings.BASE_DIR) / "primary_docs"
    if not (docs / "lfk.xlsx").exists():
        pytest.skip("нет файлов заказчика")
    ds = docs / "Gruppy_DS.xlsx"
    load_initial_catalog(docs / "lfk.xlsx", docs / "basseyn.xlsx", ds if ds.exists() else None)


# --- Шахматка: сетка и вымышленные инструкторы ----------------------------------------------
# Пара 2/2 Волков (работает 05–06.10, 09–10.10) / Лебедева (07–08.10), Соколов — 5/2.


@pytest.fixture
def slots(db) -> dict[str, InstructorSlot]:
    grid = [
        (time(9, 10), time(9, 40), False),
        (time(9, 50), time(10, 20), False),
        (time(10, 30), time(11, 0), False),
        (time(11, 10), time(11, 40), False),
        (time(18, 0), time(18, 30), True),
    ]
    return {
        f"{start:%-H:%M}": InstructorSlot.objects.create(start=start, end=end, is_evening=evening)
        for start, end, evening in grid
    }


@pytest.fixture
def staff(slots) -> dict[str, Instructor]:
    people = {
        "volkov": Instructor.objects.create(full_name="Волков В.В.", short_name="Волков"),
        "lebedeva": Instructor.objects.create(full_name="Лебедева Л.Л.", short_name="Лебедева"),
        "sokolov": Instructor.objects.create(
            full_name="Соколов С.С.", short_name="Соколов", display_order=1
        ),
    }
    set_partner(people["volkov"], people["lebedeva"])
    for key, pattern, anchor in [
        ("volkov", "2/2", date(2026, 10, 5)),
        ("lebedeva", "2/2", date(2026, 10, 7)),
        ("sokolov", "5/2", date(2026, 10, 5)),
    ]:
        ShiftPattern.objects.create(
            instructor=people[key],
            pattern=pattern,
            anchor_date=anchor,
            valid_from=date(2026, 10, 5),
        )
    return people


@pytest.fixture
def patient(doctor, make_program, procedures, staff):
    """make: пациент с индивидуальным занятием (по умолчанию курс 05.10–19.10)."""

    from apps.programs.models import Prescription
    from apps.programs.services import add_prescription

    def make(room: str = "9", per_day: int = 1, **fields):
        program = make_program(room=room, full_name=f"Пациентов{room} Тест Тестович", **fields)
        add_prescription(
            doctor,
            Prescription(program=program, procedure=procedures["individual"], per_day=per_day),
        )
        return program

    return make
