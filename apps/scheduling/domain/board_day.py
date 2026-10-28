"""Шахматка индивидуальных на один день (TZ.md §7.2, FR-SCH-6…8, 13) — чистый Python без Django.

``plan_day`` раскладывает пациентов по ячейкам одного будного дня:

1. **перенос** — пациент встаёт туда, где стоял (на предыдущем дне или сейчас на этом же):
   тот же инструктор, а у пары 2/2 — работающий напарник, то же время, та же пометка. Нельзя
   (инструктор не работает, слот закрыт занятостью, пациент в это время занят) — занятие уходит
   в «Не распределены»;
2. **автоматическая постановка** — недостающие занятия новых пациентов: свободная ячейка
   колонки, где сегодня меньше всего пациентов, затем раннее время, затем порядок колонок.
   Автоматически в ячейку ставится не больше одного пациента. Мото-Л / Артромот — одно
   занятие в вечернем слоте у инструктора 2/2 (FR-SCH-8). Если время группы пациенту ещё
   не выбрано, постановка по возможности обходит все занятия этой группы (``reserved``,
   решение 81): выбор не должен упираться в индивидуальное.

Два индивидуальных пациента не идут подряд, пациент не бывает в двух местах сразу.
Время — минуты от полуночи. ``PatientDay.why`` — почему пациент встал туда или не встал:
для журнала расписания (FR-SCH-19).
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
    name: str = ""

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
    ``reserved`` — все возможные времена групп, у которых время ещё не выбрано: автоматическая
    постановка их обходит, если может; перенос и ручная постановка их не учитывают.
    """

    program_id: int
    need: int
    seats: tuple[Seat, ...] = ()
    unplaced: int = 0
    cancelled: int = 0
    busy: tuple[tuple[int, int], ...] = ()
    evening_note: str = ""
    reserved: tuple[tuple[int, int], ...] = ()
    # Названия занятий ``busy`` в том же порядке — для пояснений («занят: Эрго общая»).
    busy_labels: tuple[str, ...] = ()

    def busy_at(self, slot: "GridSlot") -> str:
        """Чем пациент занят в это время ('' — свободен)."""
        for index, (start, end) in enumerate(self.busy):
            if start < slot.end and slot.start < end:
                label = self.busy_labels[index] if index < len(self.busy_labels) else ""
                return label or "другое занятие"
        return ""


@dataclass(frozen=True)
class PatientDay:
    program_id: int
    seats: tuple[Seat, ...] = ()
    unplaced: int = 0
    cancelled: int = 0
    # Пояснение для журнала — не часть результата: в сравнении не участвует.
    why: str = field(default="", compare=False)


@dataclass
class _State:
    patient: Patient
    seats: list[Seat] = field(default_factory=list)
    unplaced: int = 0
    cancelled: int = 0
    notes: list[str] = field(default_factory=list)


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
        elif missing:
            state.unplaced += missing
            state.notes.append(
                f"{_count(missing)} в «Не распределены»: сегодняшнее расписание сама система "
                "не заполняет — ставит специалист"
            )
    return [
        PatientDay(
            s.patient.program_id, tuple(s.seats), s.unplaced, s.cancelled, "; ".join(s.notes)
        )
        for s in states
    ]


def _missing(state: _State) -> int:
    return max(state.patient.need, 0) - len(state.seats) - state.unplaced - state.cancelled


class _Planner:
    def __init__(self, columns: list[Column], slots: list[GridSlot], fixed: tuple[Seat, ...]):
        self.columns = columns
        self.slots = sorted(slots, key=lambda s: s.start)
        self.slot = {s.id: s for s in slots}
        self.column_of = {m.id: c for c in columns for m in c.members}
        self.member = {m.id: m for c in columns for m in c.members}
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
                state.notes.append(
                    f"{self._where(seat)} убрано: индивидуальных в этот день меньше "
                    "(назначение изменилось или день без занятий)"
                )
                break
            slot = self.slot[seat.slot_id]
            moved, why = self._carry_seat(seat)
            if moved is not None:
                why = self._why_not_fits(state, slot)
            if moved is not None and not why:
                if moved.instructor_id != seat.instructor_id:
                    state.notes.append(
                        f"{self._name(seat.instructor_id)} в {_hhmm(slot.start)} не работает — "
                        f"к напарнику {self._name(moved.instructor_id)}"
                    )
                self._place(state, moved)
            else:
                failed += 1
                state.notes.append(f"{self._where(seat)}: {why} — в «Не распределены»")
        lost = len(patient.seats) - len(known)
        if lost:
            failed += lost
            state.notes.append(f"{_count(lost)}: слота больше нет в сетке — в «Не распределены»")
        free = need - len(state.seats)
        state.cancelled = min(patient.cancelled, free)
        state.unplaced = min(patient.unplaced + failed, free - state.cancelled)
        return state

    def _carry_seat(self, seat: Seat) -> tuple[Seat | None, str]:
        """Куда перенести ячейку и, если некуда, — почему."""
        column = self.column_of.get(seat.instructor_id)
        if column is None:
            return None, "инструктора нет в расписании этого дня (архив или вне смен)"
        slot = self.slot[seat.slot_id]
        # Сам инструктор, а если он не вышел — напарник по 2/2: колонка у пары общая.
        members = sorted(column.members, key=lambda m: m.id != seat.instructor_id)
        for member in members:
            if member.can_take(slot):
                return Seat(member.id, slot.id, seat.note), ""
        reasons = [
            ("" if m.id == seat.instructor_id else f"напарник {m.name or 'инструктор'} ")
            + ("не работает" if not m.working else "занят (распорядок)")
            for m in members
        ]
        return None, ", ".join(reasons)

    def _why_not_fits(self, state: _State, slot: GridSlot) -> str:
        """Почему пациенту нельзя в это время ('' — можно)."""
        busy = state.patient.busy_at(slot)
        if busy:
            return f"пациент в это время занят: {busy}"
        for seat in state.seats:
            own = self.slot[seat.slot_id]
            if max(own.start - slot.end, slot.start - own.end) <= ADJACENT_GAP:
                return f"подряд с другим индивидуальным в {_hhmm(own.start)}"
        return ""

    def _name(self, instructor_id: int) -> str:
        member = self.member.get(instructor_id)
        return member.name if member is not None and member.name else "инструктор"

    def _where(self, seat: Seat) -> str:
        slot = self.slot.get(seat.slot_id)
        time = _hhmm(slot.start) if slot is not None else "?"
        return f"{self._name(seat.instructor_id)} {time}"

    def auto(self, state: _State, missing: int) -> None:
        note = state.patient.evening_note
        evening = bool(note) and not any(self.slot[s.slot_id].evening for s in state.seats)
        for _ in range(missing):
            seat = self._best(state, evening, note if evening else "", reserved=True)
            around = True
            if seat is None:
                # Обойти возможные времена групп нельзя — ставим всё равно (решение 81).
                seat = self._best(state, evening, note if evening else "", reserved=False)
                around = False
            if seat is None:
                state.unplaced += 1
                state.notes.append(
                    f"не поставлен автоматически — {self._why_none(state, evening)}; "
                    "в «Не распределены»"
                )
            else:
                state.notes.append(self._why_here(state, seat, evening, around))
                self._place(state, seat)
            evening = False

    def _why_here(self, state: _State, seat: Seat, evening: bool, around: bool) -> str:
        slot = self.slot[seat.slot_id]
        column = self.column_of[seat.instructor_id]
        text = f"автоматически к {self._name(seat.instructor_id)} в {_hhmm(slot.start)}"
        if evening:
            text += f" — {state.patient.evening_note}: вечером у инструктора 2/2"
        else:
            text += (
                f" — у колонки наименьшая загрузка ({self.load[column.key]} пац.), "
                "самое раннее подходящее время"
            )
        blocked = self._blockers(state, evening, before=slot.start)
        if blocked:
            text += f"; раньше мешало: {blocked}"
        if not around and state.patient.reserved:
            text += "; обойти возможные времена группы без выбранного времени не удалось"
        return text

    def _why_none(self, state: _State, evening: bool) -> str:
        blocked = self._blockers(state, evening, before=None)
        if evening:
            return f"нет свободного вечернего слота у инструктора 2/2 ({blocked or 'все заняты'})"
        return f"нет подходящей ячейки ({blocked or 'все ячейки заняты'})"

    def _blockers(self, state: _State, evening: bool, *, before: int | None) -> str:
        """Что не дало поставить в другие свободные ячейки: занятия пациента, «подряд»,
        занятые ячейки и занятость инструкторов — кратко, по видам."""
        patient: dict[str, list[str]] = {}
        taken = closed = adjacent = 0
        for column in self.columns:
            for slot in self.slots:
                if slot.evening != evening or (before is not None and slot.start >= before):
                    continue
                if self.taken[(column.key, slot.id)]:
                    taken += 1
                    continue
                if not any(m.can_take(slot) and (m.evening or not evening) for m in column.members):
                    closed += 1
                    continue
                busy = state.patient.busy_at(slot)
                if busy:
                    patient.setdefault(busy, [])
                    if _hhmm(slot.start) not in patient[busy]:
                        patient[busy].append(_hhmm(slot.start))
                elif self._why_not_fits(state, slot):
                    adjacent += 1
        parts = [
            f"пациент занят: {label} ({', '.join(times[:3])})" for label, times in patient.items()
        ]
        if adjacent:
            parts.append("подряд с другим индивидуальным")
        if taken:
            parts.append(f"занято ячеек: {taken}")
        if closed:
            parts.append(f"у инструкторов занятость или не в смене: {closed}")
        return ", ".join(parts)

    def _best(self, state: _State, evening: bool, note: str, *, reserved: bool) -> Seat | None:
        best: tuple[tuple[int, int, int], Seat] | None = None
        for order, column in enumerate(self.columns):
            for slot in self.slots:
                if slot.evening != evening or self.taken[(column.key, slot.id)]:
                    continue
                if reserved and _overlaps(slot, state.patient.reserved):
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
        if _overlaps(slot, state.patient.busy):
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


def _overlaps(slot: GridSlot, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start < slot.end and slot.start < end for start, end in spans)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def _count(units: int) -> str:
    return "1 занятие" if units == 1 else f"{units} занятия"
