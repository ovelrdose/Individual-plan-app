"""Журнал расписания (FR-SCH-19): что изменилось у пациента — чистый Python без Django.

Состояние пациента — словарь ``(предмет, дата) → текст``: предмет ``ind`` — индивидуальные
(«Иванова 9:00, Не распределены»), ``eq:<id>`` — тренажёр («13:00», «Отменён»). ``changes``
сравнивает состояние до и после действия и собирает изменения подряд идущих дат с одинаковым
«было → стало» в одну запись: перенос тренажёра на весь курс — одна строка, а не четырнадцать.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

INDIVIDUAL = "ind"

Key = tuple[str, date]


@dataclass(frozen=True)
class Change:
    subject: str
    date_from: date
    date_to: date
    before: str
    after: str
    reason: str


def changes(
    before: dict[Key, str],
    after: dict[Key, str],
    reasons: dict[Key, list[str]] | None = None,
    base: dict[date, date] | None = None,
) -> list[Change]:
    """Изменения по предметам и диапазонам дат.

    ``base`` — новые дни шахматки и дни, с которых они составлены: индивидуальные нового дня
    сравниваются с тем днём, а не с пустотой (иначе каждый день в журнал шёл бы каждый
    пациент «как вчера», решение 86). Тренажёры стоят на весь курс — их это не касается."""
    reasons = reasons or {}
    base = base or {}
    dates: dict[str, set[date]] = defaultdict(set)
    for subject, day in (*before, *after):
        dates[subject].add(day)
    result: list[Change] = []
    for subject in sorted(dates):
        run: list[tuple[date, str, str]] = []
        for day in sorted(dates[subject]):
            if subject == INDIVIDUAL and day in base:
                old = before.get((subject, base[day]), "")
            else:
                old = before.get((subject, day), "")
            new = after.get((subject, day), "")
            if old == new:
                result += _close(subject, run, reasons)
                run = []
                continue
            if run and (run[-1][1], run[-1][2]) != (old, new):
                result += _close(subject, run, reasons)
                run = []
            run.append((day, old, new))
        result += _close(subject, run, reasons)
    return result


def _close(
    subject: str, run: list[tuple[date, str, str]], reasons: dict[Key, list[str]]
) -> list[Change]:
    if not run:
        return []
    texts: list[str] = []
    for day, _old, _new in run:
        for text in reasons.get((subject, day), []):
            if text and text not in texts:
                texts.append(text)
    _first, old, new = run[0]
    return [Change(subject, run[0][0], run[-1][0], old, new, "; ".join(texts))]


@dataclass(frozen=True)
class Shown:
    """Запись для показа: что ушло, что пришло и что осталось как было."""

    before: str
    after: str
    kept: str


def shown(before: str, after: str) -> Shown:
    """Выделить в «было → стало» только изменившиеся занятия.

    Состояние индивидуальных — все занятия дня: у пациента с двумя инд. перенос одного
    выглядел бы как «Волков 9:10, Соколов 15:40 → Волков 9:10, Волков 15:40», и не видно, какое
    перенесли. Показываем «Соколов 15:40 → Волков 15:40», остальное — «без изменений»."""
    old, new = _items(before), _items(after)
    kept: list[str] = []
    added: list[str] = []
    for item in new:
        if item in old:
            old.remove(item)
            kept.append(item)
        else:
            added.append(item)
    if not old and not added:
        return Shown(before, after, "")
    return Shown(", ".join(old), ", ".join(added), ", ".join(kept))


def _items(text: str) -> list[str]:
    """Занятия через «, »; запятая в пометке («Волков 9:10 (art, шар)») — не разделитель."""
    items: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0 and text[index + 1 : index + 2] == " ":
            items.append(text[start:index])
            start = index + 2
    items.append(text[start:])
    return [item for item in items if item]
