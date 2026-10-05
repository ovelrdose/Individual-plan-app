"""Начальное наполнение справочников (TZ.md, FR-CAT-6).

Названия групп в lfk.xlsx и basseyn.xlsx записаны по-разному («Эрго - общая», «I can нога »),
поэтому файл сопоставляется с описаниями ниже по нормализованному названию.
Время занятий берётся из файлов, всё остальное — отсюда.
"""

from dataclasses import dataclass, field
from datetime import time

from .domain import normalize_name
from .models import ProcedureKind

DEPARTMENT_CODE = "omr4"
DEPARTMENT_NAME = "ОМР № 4"


@dataclass(frozen=True)
class ProcedureSpec:
    name: str
    card_label: str
    kind: ProcedureKind
    duration_min: int | None = None
    place: str = ""
    synonyms: tuple[str, ...] = ()
    # Названия в файле расписания, которые означают эту группу.
    file_names: tuple[str, ...] = field(default=())
    group_choice: bool = False

    def matches(self, file_name: str) -> bool:
        return normalize_name(file_name) in {normalize_name(n) for n in self.file_names}


LFK = ProcedureKind.LFK_GROUP
LFK_GROUPS = (
    ProcedureSpec("Эрго общая", "Группа Эрго общая", LFK, 30, "зал ЛФК",
                  ("Эрго общая",), ("Эрго - общая",)),
    ProcedureSpec("Вестибулярная гимнастика", "Группа Вест.гимнастика", LFK, 30, "эргозона",
                  ("Вестибулярная гимнастика", "Вест. гимнастика"), ("Вестибулярная гимнастика",)),
    ProcedureSpec("I can нога", "Группа I can нога", LFK, 30, "зал ЛФК",
                  ("I can нога", "i-can нога"), ("I can нога",)),
    ProcedureSpec("Нейро-тренинг", "Нейротренинг", LFK, 30, "эргозона",
                  ("Нейро-тренинг",), ("Нейро - тренинг",)),
    ProcedureSpec("Лого-тренинг", "Лого-тренинг", LFK, 30, "",
                  ("Лого-тренинг",), ("Лого - тренинг",)),
    # В lfk.xlsx группа в 13:40 называется «Эрготерапия», в картах — «Эрготренинг».
    ProcedureSpec("Эрготерапия", "Эрготренинг", LFK, 30, "эргозона",
                  ("Эрготерапия", "Эрготренинг"), ("Эрготерапия",)),
    ProcedureSpec("Логоритмика", "Логоритмика", LFK, 30, "",
                  ("Логоритмика",), ("Логоритмика",)),
    ProcedureSpec("I can рука", "Группа I can рука", LFK, 30, "эргозона",
                  ("I can рука", "i-can рука"), ("I can рука",)),
    ProcedureSpec("Эрго кисть", "Группа Эрго кисть", LFK, 30, "эргозона",
                  ("Эрго кисть",), ("Эрго - кисть",)),
)  # fmt: skip

POOL = ProcedureKind.POOL
POOL_GROUPS = (
    ProcedureSpec("ЛФК в воде: верхняя конечность", "ЛФК в воде", POOL, 30, "бассейн",
                  ("ЛФК в воде (верхняя конечность)",), ("Верхняя конечность",)),
    ProcedureSpec("ЛФК в воде: нижняя конечность", "ЛФК в воде", POOL, 30, "бассейн",
                  ("ЛФК в воде (нижняя конечность)",), ("Нижняя конечность",)),
    ProcedureSpec("ЛФК в воде: спина", "ЛФК в воде", POOL, 30, "бассейн",
                  ("ЛФК в воде (спина)",), ("Спина",)),
)  # fmt: skip

# Назначение «бассейн» без группы — группу выбирает специалист ФР (FR-CAT-2).
POOL_UNSPECIFIED = ProcedureSpec(
    "Бассейн", "ЛФК в воде", POOL, 30, "бассейн", ("Бассейн", "ЛФК в воде"), group_choice=True
)

INDIVIDUAL = ProcedureSpec(
    "Индивидуальное занятие",
    "Инд.занятие",
    ProcedureKind.INDIVIDUAL,
    30,
    "",
    ("Индивидуальное занятие", "Индивидуальное занятие ЛФК", "Инд. занятие"),
)


@dataclass(frozen=True)
class EquipmentSpec:
    name: str
    synonyms: tuple[str, ...]


EQUIPMENT_PLACE = "зона БОС терапии"
EQUIPMENT = (
    EquipmentSpec("st-150", ("ST-150", "ST – 150", "st 150")),
    EquipmentSpec("Имитрон", ("Имитрон",)),
    EquipmentSpec("Pablo", ("Pablo",)),
)

CARD = ProcedureKind.CARD_ONLY
CARD_ONLY = (
    ProcedureSpec("T.tigo", "T.tigo", CARD, 15, "", ("Thera Trainer Tigo", "Tigo")),
    ProcedureSpec("Алмаг", "Алмаг", CARD, 20, "", ("Алмаг",)),
    ProcedureSpec("Степпер", "Степпер", CARD, 15, "", ("Степпер",)),
    ProcedureSpec("Массаж", "Массаж", CARD, None, "", ("Массаж",)),
    ProcedureSpec("Орторент-дорожка", "Орторент-дорожка", CARD, None, "", ("Орторент",)),
    ProcedureSpec("Механотерапия", "механотерапия", CARD, None, "", ("Механотерапия",)),
    # Консультации — занятие один на один со специалистом, в карте только строка для подписи.
    # Не путать «Эрготерапевт» с группой «Эрготерапия» (в карте «Эрготренинг»).
    ProcedureSpec("Эрготерапевт", "Эрготерапевт", CARD, None, "",
                  ("Эрготерапевт", "Консультация эрготерапевта")),
    ProcedureSpec("Психолог", "Психолог", CARD, None, "", ("Психолог", "Консультация психолога")),
    ProcedureSpec("Логопед", "Логопед", CARD, None, "", ("Логопед", "Консультация логопеда")),
)  # fmt: skip

# Сетка слотов инструкторов (FR-CAT-5): (начало, конец, вечерний).
SLOTS = (
    (time(9, 10), time(9, 40), False),
    (time(9, 50), time(10, 20), False),
    (time(10, 30), time(11, 0), False),
    (time(11, 10), time(11, 40), False),
    (time(13, 0), time(13, 30), False),
    (time(13, 40), time(14, 10), False),
    (time(14, 20), time(14, 50), False),
    (time(15, 0), time(15, 30), False),
    (time(15, 40), time(16, 10), False),
    (time(16, 20), time(16, 50), False),
    (time(18, 0), time(18, 30), True),
    (time(18, 40), time(19, 10), True),
    (time(19, 20), time(19, 50), True),
)
