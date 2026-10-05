"""Данные для заполнения карты. Не зависят ни от Django, ни от шаблона."""

from dataclasses import dataclass
from datetime import date, time


@dataclass(frozen=True)
class ScheduleItem:
    """Строка блока «Расписание занятий»: слот сетки в типичном дне.

    Пустая подпись — окно: время печатается, а занятие инструктор впишет сам.
    """

    start: time
    label: str
    place: str = ""


@dataclass(frozen=True)
class CardProcedure:
    """Строка нижнего блока «Уважаемый пациент!».

    Даты вне [start_date, cancel_date) заштриховываются. Смысл полей — как у назначения:
    None в start_date — с первого дня курса, None в cancel_date — до конца курса.
    """

    label: str
    start_date: date | None = None
    cancel_date: date | None = None

    def is_active(self, day: date) -> bool:
        if self.start_date is not None and day < self.start_date:
            return False
        return self.cancel_date is None or day < self.cancel_date


@dataclass(frozen=True)
class CardData:
    department: str
    full_name: str
    sex: str
    age: int | None
    doctor: str
    course_dates: tuple[date, ...]
    schedule: tuple[ScheduleItem, ...]
    procedures: tuple[CardProcedure, ...]
