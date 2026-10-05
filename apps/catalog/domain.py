"""Правила справочников без Django: время, шаги, нормализация названий."""

import re
from datetime import date, datetime, time, timedelta


def add_minutes(start: time, minutes: int) -> time:
    moment = datetime.combine(date.min, start) + timedelta(minutes=minutes)
    if moment.date() != date.min:
        raise ValueError("Время выходит за пределы суток.")
    return moment.time()


def minutes_between(start: time, end: time) -> int:
    delta = datetime.combine(date.min, end) - datetime.combine(date.min, start)
    return int(delta.total_seconds() // 60)


def equipment_starts(
    window_start: time, window_end: time, step_min: int, duration_min: int
) -> list[time]:
    """Возможные начала записи на тренажёр: window_start + k·step, конец не позже window_end
    (TZ.md, FR-CAT-4)."""
    if step_min <= 0 or duration_min <= 0:
        raise ValueError("Шаг и длительность должны быть положительными.")
    starts = []
    offset = 0
    while minutes_between(window_start, window_end) - offset >= duration_min:
        starts.append(add_minutes(window_start, offset))
        offset += step_min
    return starts


def excel_time(value: object) -> time:
    """Время из ячейки Excel: time, datetime или доля суток (0.381944… = 09:10).

    Доля суток округляется до минуты — в файлах заказчика встречаются хвосты вроде 0.3819444.
    """
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, int | float) and 0 <= value < 1:
        total = round(value * 24 * 60)
        return time(total // 60, total % 60)
    raise ValueError(f"Не удалось прочитать время: {value!r}")


_DASHES_SPACES_DOTS = re.compile(r"[\s.\-‐‑‒–—―]+")


def normalize_name(text: str) -> str:
    """Ключ для сравнения названий (TZ.md, FR-IMP-6): нижний регистр, ё→е,
    без пробелов, точек и любых тире. «ST – 150» и «st-150» → «st150»."""
    return _DASHES_SPACES_DOTS.sub("", text.lower().replace("ё", "е"))
