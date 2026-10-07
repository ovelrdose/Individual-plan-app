"""Темы событий и кому они нужны (TZ.md NFR-11). Чистый Python, без Django.

Тема — что изменилось: ``board:2026-10-07`` — шахматка на дату, ``board`` — все шахматки
(смена или распорядок инструктора), ``program:13`` — программа, ``programs`` — список программ.
"""

from collections.abc import Iterable

BOARD = "board"
PROGRAMS = "programs"


def board(day: object) -> str:
    return f"{BOARD}:{day}"


def program(pk: int) -> str:
    return f"program:{pk}"


def matches(subscribed: Iterable[str], topics: Iterable[str]) -> bool:
    """Нужно ли событие странице: совпала тема, или изменились все шахматки, а страница
    смотрит одну, или страница смотрит все шахматки, а изменилась одна."""
    wanted = set(subscribed)
    for topic in topics:
        if topic in wanted:
            return True
        if topic == BOARD and any(item.startswith(f"{BOARD}:") for item in wanted):
            return True
        if topic.startswith(f"{BOARD}:") and BOARD in wanted:
            return True
    return False


def parse(value: str, *, allowed: tuple[str, ...] | None = None) -> list[str]:
    """Темы из адреса потока: «board:2026-10-07,program:13». ``allowed`` — допустимые
    префиксы (публичной шахматке — только шахматки)."""
    result = []
    for item in value.split(","):
        item = item.strip()
        if not item or len(item) > 40:
            continue
        if allowed is not None and not item.startswith(allowed):
            continue
        result.append(item)
    return result[:10]
