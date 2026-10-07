"""Шахматка индивидуальных на один день (TZ.md §7.2, FR-SCH-6…8, 13) — чистый Python без Django.

``plan_day`` раскладывает пациентов по ячейкам одного будного дня:

1. **перенос** — пациент встаёт туда, где стоял (на предыдущем дне или сейчас на этом же):
   тот же инструктор, а у пары 2/2 — работающий напарник, то же время, та же пометка. Нельзя
   (инструктор не работает, слот закрыт занятостью, пациент в это время занят) — занятие уходит
   в «Не распределены»;
2. **автоматическая постановка** — недостающие занятия новых пациентов: свободная ячейка
   колонки, где сегодня меньше всего пациентов, затем раннее время, затем порядок колонок.
   Автоматически в ячейку ставится не больше одного пациента. Мото-Л / Артромот — одно
   занятие в вечернем слоте у инструктора 2/2 (FR-SCH-8).

Два индивидуальных пациента не идут подряд, пациент не бывает в двух местах сразу.
Время — минуты от полуночи.
"""

from collections import Counter
from dataclasses import dataclass, field

from .model import ADJACENT_GAP


@dataclass(frozen=True)
class GridSlot:
    id: int
    start: int
    end: int
    evening: bool = False


@dataclass(frozen=True)
class Member:
    """Инструктор в этот день. ``free`` — слоты без постоянной занятости (пусто, если не
    работает), ``evening`` — может вести вечером: 2/2, работает 8:00–20:00."""

    id: int
    working: bool
    free: frozenset[int] = frozenset()
    evening: bool = False

    def can_take(self, slot: GridSlot) -> bool:
        return self.working and slot.id in self.free


@dataclass(frozen=True)
class Column:
    """Колонка шахматки: одиночный инструктор или пара 2/2, в порядке шахматки."""

    key: str
    members: tuple[Member, ...]


@dataclass(frozen=True)
class Seat:
    """Пациент в ячейке: инструктор, слот, пометка («Мото-Л»)."""

    instructor_id: int
    slot_id: int
    note: str = ""


@dataclass(frozen=True)
class Patient:
    """Пациент на дату.

    ``need`` — сколько индивидуальных в этот день (0 — день без занятий: поступление, выписка,
    индивидуальное отменено). ``seats``, ``unplaced``, ``cancelled`` — откуда переносим:
    ячейки и блоки «Не распределены» / «Отменены». ``busy`` — другие занятия пациента в этот
    день (группы, бассейн, тренажёры). ``evening_note`` — назначен Мото-Л / Артромот.
    """

    program_id: int
    need: int
    seats: tuple[Seat, ...] = ()
    unplaced: int = 0
    cancelled: int = 0
    busy: tuple[tuple[int, int], ...] = ()
    evening_note: str = ""


@dataclass(frozen=True)
class PatientDay:
    program_id: int
    seats: tuple[Seat, ...] = ()
    unplaced: int = 0
    cancelled: int = 0


@dataclass
class _State:
    patient: Patient
    seats: list[Seat] = field(default_factory=list)
    unplaced: int = 0
    cancelled: int = 0


def plan_day(
    patients: list[Patient],
    columns: list[Column],
    slots: list[GridSlot],
    *,
    fixed: tuple[Seat, ...] = (),
    auto: bool = True,
) -> list[PatientDay]:
    """Раскладывает ``patients`` по ячейкам дня в их порядке.

    ``fixed`` — пациенты, которых не трогаем (они занимают ячейки и нагрузку колонок).
    ``auto = False`` — без автоматической постановки: недостающее сразу в «Не распределены»
    (сегодняшняя шахматка, FR-SCH-9).
    """
    planner = _Planner(columns, slots, fixed)
    states = [planner.carry(patient) for patient in patients]
    for state in states:
        missing = _missing(state)
        if missing and auto:
            planner.auto(state, missing)
        else:
            state.unplaced += missing
    return [
        PatientDay(s.patient.program_id, tuple(s.seats), s.unplaced, s.cancelled) for s in states
    ]


def _missing(state: _State) -> int:
    return max(state.patient.need, 0) - len(state.seats) - state.unplaced - state.cancelled


class _Planner:
    def __init__(self, columns: list[Column], slots: list[GridSlot], fixed: tuple[Seat, ...]):
        self.columns = columns
        self.slots = sorted(slots, key=lambda s: s.start)
        self.slot = {s.id: s for s in slots}
        self.column_of = {m.id: c for c in columns for m in c.members}
        self.taken: Counter[tuple[str, int]] = Counter()
        self.load: Counter[str] = Counter()
        for seat in fixed:
            self._occupy(seat)

    def _occupy(self, seat: Seat) -> None:
        column = self.column_of.get(seat.instructor_id)
        if column is not None:
            self.taken[(column.key, seat.slot_id)] += 1
            self.load[column.key] += 1

    def carry(self, patient: Patient) -> _State:
        """Перенос: ячейки — пока хватает занятий на день, затем блоки (ячейки важнее)."""
        state = _State(patient)
        need = max(patient.need, 0)
        failed = 0
        known = [s for s in patient.seats if s.slot_id in self.slot]
        for seat in sorted(known, key=lambda s: self.slot[s.slot_id].start):
            if len(state.seats) >= need:
                break
            moved = self._carry_seat(seat)
            if moved is not None and self._fits(state, self.slot[moved.slot_id]):
                self._place(state, moved)
            else:
                failed += 1
        failed += len(patient.seats) - len(known)
        free = need - len(state.seats)
        state.cancelled = min(patient.cancelled, free)
        state.unplaced = min(patient.unplaced + failed, free - state.cancelled)
        return state

    def _carry_seat(self, seat: Seat) -> Seat | None:
        column = self.column_of.get(seat.instructor_id)
        if column is None:
            return None
        slot = self.slot[seat.slot_id]
        # Сам инструктор, а если он не вышел — напарник по 2/2: колонка у пары общая.
        members = sorted(column.members, key=lambda m: m.id != seat.instructor_id)
        for member in members:
            if member.can_take(slot):
                return Seat(member.id, slot.id, seat.note)
        return None

    def auto(self, state: _State, missing: int) -> None:
        note = state.patient.evening_note
        evening = bool(note) and not any(self.slot[s.slot_id].evening for s in state.seats)
        for _ in range(missing):
            seat = self._best(state, evening, note if evening else "")
            if seat is None:
                state.unplaced += 1
            else:
                self._place(state, seat)
            evening = False

    def _best(self, state: _State, evening: bool, note: str) -> Seat | None:
        best: tuple[tuple[int, int, int], Seat] | None = None
        for order, column in enumerate(self.columns):
            for slot in self.slots:
                if slot.evening != evening or self.taken[(column.key, slot.id)]:
                    continue
                member = next(
                    (m for m in column.members if m.can_take(slot) and (m.evening or not evening)),
                    None,
                )
                if member is None or not self._fits(state, slot):
                    continue
                key = (self.load[column.key], slot.start, order)
                if best is None or key < best[0]:
                    best = (key, Seat(member.id, slot.id, note))
        return best[1] if best else None

    def _fits(self, state: _State, slot: GridSlot) -> bool:
        """Пациент свободен в это время, и это не второе индивидуальное подряд."""
        for start, end in state.patient.busy:
            if start < slot.end and slot.start < end:
                return False
        for seat in state.seats:
            own = self.slot[seat.slot_id]
            gap = max(own.start - slot.end, slot.start - own.end)
            if gap <= ADJACENT_GAP:
                return False
        return True

    def _place(self, state: _State, seat: Seat) -> None:
        state.seats.append(seat)
        self._occupy(seat)
