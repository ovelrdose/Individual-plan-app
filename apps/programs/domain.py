"""Правила программы без Django: курс, палаты, поиск, пол по отчеству (TZ.md §5, FR-IMP-7)."""

from collections.abc import Iterable
from datetime import date, timedelta


def course_end(start_date: date, course_days: int) -> date:
    """Последний день курса: курс считается в календарных днях, включая первый день."""
    if course_days < 1:
        raise ValueError("Длительность курса — не меньше одного дня.")
    return start_date + timedelta(days=course_days - 1)


def course_dates(start_date: date, end_date: date) -> list[date]:
    days = (end_date - start_date).days + 1
    return [start_date + timedelta(days=offset) for offset in range(max(days, 0))]


# Отсутствие пациента: с даты выбытия до даты возврата (не включая; None — не вернулся).
Gap = tuple[date, date | None]


def is_absent(gaps: Iterable[Gap], day: date) -> bool:
    """Пациент выбыл и в этот день не лечится (TZ.md, FR-PRG-9)."""
    return any(start <= day and (back is None or day < back) for start, back in gaps)


def is_therapy_day(start_date: date, end_date: date, day: date, gaps: Iterable[Gap] = ()) -> bool:
    """День занятий: в день поступления и в день выписки занятий нет никаких (TZ.md, FR-SCH-1),
    после выбытия — тоже (FR-PRG-9)."""
    return start_date < day < end_date and not is_absent(gaps, day)


def therapy_dates(start_date: date, end_date: date, gaps: Iterable[Gap] = ()) -> list[date]:
    gaps = list(gaps)
    return [
        day for day in course_dates(start_date, end_date)
        if is_therapy_day(start_date, end_date, day, gaps)
    ]  # fmt: skip


def room_label(room: str) -> str:
    """Палата в шахматке: «9п», а вариант палаты — как на бумаге, без «п»: «5а» (глоссарий,
    «Nп Фамилия»)."""
    room = room.strip()
    return room if room[-1:].isalpha() else f"{room}п"


def room_sort_key(room: str) -> tuple[int, str]:
    """Палаты по номеру, а не как строки: 5, 5а, 9, 12 (а не 12, 5а, 9)."""
    digits = "".join(ch for ch in room if ch.isdigit())
    return (int(digits) if digits else 10**6, room.lower())


def fold(text: str) -> str:
    """Для поиска: без регистра, «ё» = «е» (Семёнов находится по «семенов»)."""
    return text.lower().replace("ё", "е")


def patient_key(full_name: str) -> str:
    """Тот же пациент по ФИО — без регистра, «ё» и лишних пробелов (FR-IMP-11, FR-PRG-10)."""
    return " ".join(fold(full_name).split())


def surname(full_name: str) -> str:
    """Фамилия — первое слово ФИО. Нужна для шахматки («9п Иванов») и имени файла карты."""
    parts = full_name.split()
    return parts[0] if parts else ""


MALE, FEMALE = "М", "Ж"


def sex_from_name(full_name: str) -> str:
    """Пол по отчеству (FR-IMP-7). Пустая строка — определить не удалось.

    Иванович, Ильич, Кузьмич, Мамед оглы → М; Ивановна, Ильинична, Никитична, Лейла кызы → Ж.
    """
    parts = fold(full_name).split()
    if len(parts) < 3:
        return ""
    if parts[-1] == "оглы":
        return MALE
    if parts[-1] == "кызы":
        return FEMALE
    patronymic = parts[2]
    if patronymic.endswith(("вна", "чна")):
        return FEMALE
    if patronymic.endswith(("вич", "ич")):
        return MALE
    return ""
