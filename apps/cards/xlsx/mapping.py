"""Адреса ячеек шаблона карты (TZ.md §8.1).

Хранятся в JSON, чтобы новый шаблон отделения подключался без изменения кода.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from openpyxl.utils import column_index_from_string, get_column_letter


@dataclass(frozen=True)
class ScheduleArea:
    first_row: int
    last_row: int
    time_col: str
    label_col: str
    place_col: str

    @property
    def rows(self) -> range:
        return range(self.first_row, self.last_row + 1)


@dataclass(frozen=True)
class DateBlock:
    """Таблица отметок: строка дат и строки процедур под ней.

    У ШРМ 5 таких таблиц две (по 10 дат), процедуры повторяются в каждой.
    """

    date_row: int
    first_col: str
    size: int
    first_procedure_row: int
    last_procedure_row: int

    @property
    def date_columns(self) -> list[str]:
        first = column_index_from_string(self.first_col)
        return [get_column_letter(first + i) for i in range(self.size)]

    @property
    def procedure_rows(self) -> range:
        return range(self.first_procedure_row, self.last_procedure_row + 1)


@dataclass(frozen=True)
class CardMapping:
    department: str
    full_name: str
    sex_age: str
    doctor: str
    schedule: ScheduleArea
    date_blocks: tuple[DateBlock, ...]
    procedure_label_col: str = "A"

    @property
    def max_dates(self) -> int:
        return sum(block.size for block in self.date_blocks)

    @property
    def max_procedures(self) -> int:
        return min(len(block.procedure_rows) for block in self.date_blocks)

    @classmethod
    def from_dict(cls, data: dict) -> "CardMapping":
        return cls(
            department=data["department"],
            full_name=data["full_name"],
            sex_age=data["sex_age"],
            doctor=data["doctor"],
            schedule=ScheduleArea(**data["schedule"]),
            date_blocks=tuple(DateBlock(**block) for block in data["date_blocks"]),
            procedure_label_col=data.get("procedure_label_col", "A"),
        )


def load_mappings(path: Path) -> dict[int, CardMapping]:
    """Читает файл вида {"3": {...}, "4": {...}} → {3: CardMapping, ...}."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(key): CardMapping.from_dict(value) for key, value in raw.items()}
