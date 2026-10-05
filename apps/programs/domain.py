"""Правила программы без Django: курс, палаты, поиск, пол по отчеству (TZ.md §5, FR-IMP-7)."""

from datetime import date, timedelta


def course_end(start_date: date, course_days: int) -> date:
    """Последний день курса: курс считается в календарных днях, включая первый день."""
    if course_days < 1:
        raise ValueError("Длительность курса — не меньше одного дня.")
    return start_date + timedelta(days=course_days - 1)


def course_dates(start_date: date, end_date: date) -> list[date]:
    days = (end_date - start_date).days + 1
    return [start_date + timedelta(days=offset) for offset in range(max(days, 0))]


def room_sort_key(room: str) -> tuple[int, str]:
    """Палаты по номеру, а не как строки: 5, 5а, 9, 12 (а не 12, 5а, 9)."""
    digits = "".join(ch for ch in room if ch.isdigit())
    return (int(digits) if digits else 10**6, room.lower())


def fold(text: str) -> str:
    """Для поиска: без регистра, «ё» = «е» (Семёнов находится по «семенов»)."""
    return text.lower().replace("ё", "е")


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
