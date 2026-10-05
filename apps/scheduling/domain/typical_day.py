"""Типичный день программы (TZ.md, FR-SCH-6): то, что печатается в верхнем блоке карты.

Для каждого назначения берётся время, которое встречается в большинстве дней курса.
Дни с другим временем — отклонения: в карте они не видны, специалист видит их на экране.
У индивидуальных занятий в сравнение входит и инструктор (команда 2/2 — «Волков/ Лебедева»):
день у другого инструктора — тоже отклонение.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class ScheduledItem:
    """Одно занятие программы в конкретный день."""

    key: int  # назначение (одна строка типичного дня на назначение и его «порядковое» место)
    label: str
    place: str
    date: date
    start: int
    end: int
    # Инструктор индивидуального занятия (подпись команды); у остальных пусто.
    who: str = ""


@dataclass(frozen=True)
class TypicalRow:
    key: int
    label: str
    place: str
    start: int
    end: int
    days: int
    deviations: tuple[date, ...] = field(default=())
    who: str = ""


def typical_day(items: list[ScheduledItem]) -> list[TypicalRow]:
    """Строки типичного дня по времени. У назначения с несколькими занятиями в день
    (тренажёр 2 р/д) — по строке на каждое время."""
    by_key: dict[int, list[ScheduledItem]] = defaultdict(list)
    for item in items:
        by_key[item.key].append(item)

    rows = []
    for key, entries in by_key.items():
        per_day: dict[date, list[ScheduledItem]] = defaultdict(list)
        for entry in entries:
            per_day[entry.date].append(entry)
        units = max(len(day) for day in per_day.values())
        for unit in range(units):
            # «Первое занятие дня», «второе занятие дня» — отдельные строки типичного дня.
            slots = {
                day: sorted(day_items, key=lambda i: i.start)[unit]
                for day, day_items in per_day.items()
                if len(day_items) > unit
            }
            counts = Counter((i.start, i.end, i.place) for i in slots.values())
            typical, _ = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            start, end, place = typical
            # Инструктор — самый частый в типичное время. Пустой (индивидуальное в выходной
            # ведёт дежурный 2/2, решение 56) — не отклонение.
            whos = Counter(
                i.who for i in slots.values() if i.who and (i.start, i.end, i.place) == typical
            )
            who = min(whos.items(), key=lambda kv: (-kv[1], kv[0]))[0] if whos else ""
            sample = next(iter(slots.values()))
            deviations = tuple(
                sorted(
                    day
                    for day, i in slots.items()
                    if (i.start, i.end, i.place) != typical or (i.who and i.who != who)
                )
            )
            rows.append(
                TypicalRow(key, sample.label, place, start, end, len(slots), deviations, who)
            )
    return sorted(rows, key=lambda row: (row.start, row.label))


@dataclass(frozen=True)
class GridRow:
    """Строка блока «Расписание занятий» карты: дневной слот сетки инструкторов.

    Строка без занятий — «окно»: в это время пациент свободен, туда инструкторы ставят
    индивидуальные занятия (решение владельца 04.10, TZ.md FR-CRD-2).
    """

    slot_start: int
    items: tuple[TypicalRow, ...] = ()

    @property
    def is_window(self) -> bool:
        return not self.items

    @property
    def start(self) -> int:
        # Занятие не всегда начинается ровно по слоту (группа в 9:00 — в строке слота 9:10):
        # пациенту нужно настоящее время.
        return min((row.start for row in self.items), default=self.slot_start)

    @property
    def label(self) -> str:
        # Время у строки одно — у занятий, которые начинаются позже, оно пишется в подписи,
        # иначе пациент его не узнает («st-150/Имитрон 13:15»).
        return "/".join(
            row.label if row.start == self.start else f"{row.label} {_hhmm(row.start)}"
            for row in self.items
        )

    @property
    def place(self) -> str:
        return "/".join(dict.fromkeys(row.place for row in self.items if row.place))

    @property
    def who(self) -> str:
        """Инструктор индивидуального занятия в этом слоте — для экрана (FR-SCH-6)."""
        return "/".join(dict.fromkeys(row.who for row in self.items if row.who))

    @property
    def deviations(self) -> int:
        return sum(len(row.deviations) for row in self.items)


def day_grid(rows: list[TypicalRow], slots: list[tuple[int, int]]) -> list[GridRow]:
    """Типичный день, разложенный по дневной сетке слотов, — с окнами, как в шаблоне карты.

    Занятие попадает в слот, с которым больше всего пересекается (бассейн 13:30–14:00 —
    в слот 13:40). Несколько занятий в одном слоте — одна строка через «/», как
    «Pablo/st-150» в шаблоне. Занятие, которое не пересекается ни с одним слотом (вечернее,
    в обед), — отдельная строка со своим временем: иначе оно заняло бы чужое окно.
    Без сетки слотов окон не построить — каждое занятие идёт своей строкой.
    """
    ordered = sorted(slots)
    placed: dict[int, list[TypicalRow]] = defaultdict(list)
    extra = []
    for row in rows:
        overlaps = [_overlap(row.start, row.end, *slot) for slot in ordered]
        if not any(overlaps):
            extra.append(GridRow(row.start, (row,)))
            continue
        placed[overlaps.index(max(overlaps))].append(row)
    grid = [
        GridRow(start, tuple(sorted(placed[index], key=lambda r: (r.start, r.label))))
        for index, (start, _end) in enumerate(ordered)
    ]
    return sorted(grid + extra, key=lambda r: r.slot_start)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def _overlap(start: int, end: int, slot_start: int, slot_end: int) -> int:
    return max(0, min(end, slot_end) - max(start, slot_start))
