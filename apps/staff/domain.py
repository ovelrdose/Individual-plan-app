"""Смены и занятость инструкторов — чистый Python без Django (TZ.md, FR-STF-2…5).

Этим модулем пользуются подбор индивидуальных занятий (шаг 3.2) и шахматка (шаг 3.3).
Данные из базы собирает ``apps.staff.services.instructor_calendars``; здесь только правила.

Основной API::

    calendar = InstructorCalendar(patterns=..., exceptions=..., duties=..., blocks=...,
                                  slots=..., start=..., end=...)
    calendar.is_working(day)          # работает ли инструктор в дату
    day = calendar.day(day)           # InstructorDay: working + busy {slot_id: Busy}
    day.is_free(slot_id)              # можно ли поставить пациента в слот
    day.busy.get(slot_id)             # Busy(kind, label, text) — чем занят слот, или None

    teams = [Team.of(...), ...]       # команды шахматки: одиночка или пара 2/2
    team_candidates(team, calendars, day, slot_id)  # кто из команды может взять слот

Календарь знает, на какие даты загружены данные (``start…end``): вопрос о дате вне диапазона —
``ValueError``, а не тихий «не работает».

Правила:

- рабочий день — по шаблону смены, действующему в дату (``valid_from…valid_to`` включительно,
  ``valid_to = None`` — бессрочно); нет шаблона — день нерабочий;
- исключение на дату (``ShiftException``) важнее шаблона (FR-STF-3);
- постоянный распорядок (``InstructorDuty``) занимает слот в каждый рабочий день своего периода
  (FR-STF-5); ведение группы занимает все слоты, с которыми пересекается занятие группы
  (Лого-тренинг 11:20–11:50 закрывает и 11:10–11:40) — иначе подбор поставил бы пациента
  в пересекающийся слот;
- разовый блок на дату (``InstructorBlock``) снимает целиком обязанности, занимающие его слоты,
  и занимает их сам; блок ``CLEAR`` только снимает — слоты освобождаются;
- в нерабочий день занятости нет (``busy`` пуст), но и свободных слотов нет: ``is_free`` — False.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, time
from enum import StrEnum


class Pattern(StrEnum):
    """Шаблон смены (FR-STF-2)."""

    TWO_TWO = "2/2"
    FIVE_TWO = "5/2"
    DAILY = "daily"


class DutyKind(StrEnum):
    """Чем занят слот инструктора, кроме пациентов (FR-STF-5)."""

    METHOD_WORK = "METHOD_WORK"
    GROUP_LEAD = "GROUP_LEAD"
    BOS = "BOS"
    OTHER = "OTHER"
    # Только для разового блока: снять распорядок в этот день.
    CLEAR = "CLEAR"


DUTY_TEXT = {
    DutyKind.METHOD_WORK: "Метод. работа",
    DutyKind.GROUP_LEAD: "Ведение группы",
    DutyKind.BOS: "БОС",
    DutyKind.OTHER: "Прочее",
    DutyKind.CLEAR: "Снять распорядок",
}


def in_period(day: date, valid_from: date, valid_to: date | None) -> bool:
    """Период действия включает обе границы; ``valid_to = None`` — бессрочно."""
    return valid_from <= day and (valid_to is None or day <= valid_to)


def periods_overlap(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    """Пересекаются ли периоды ``[a_from, a_to]`` и ``[b_from, b_to]`` (None — бессрочно)."""
    return (a_to is None or b_from <= a_to) and (b_to is None or a_from <= b_to)


def pattern_works(pattern: Pattern | str, anchor_date: date, day: date) -> bool:
    """Рабочий ли день по шаблону без учёта периода и исключений (FR-STF-2).

    2/2: рабочий, если ``(day − anchor_date).days mod 4 ∈ {0, 1}`` — работает и для дат раньше
    якоря, потому что остаток в Python неотрицательный. 5/2: понедельник–пятница.
    """
    pattern = Pattern(pattern)
    if pattern is Pattern.TWO_TWO:
        return (day - anchor_date).days % 4 in (0, 1)
    if pattern is Pattern.FIVE_TWO:
        return day.weekday() < 5
    return True


@dataclass(frozen=True)
class ShiftRule:
    """Шаблон смены с периодом действия (``ShiftPattern``)."""

    pattern: Pattern
    anchor_date: date
    valid_from: date
    valid_to: date | None = None

    def covers(self, day: date) -> bool:
        return in_period(day, self.valid_from, self.valid_to)

    def works(self, day: date) -> bool:
        return self.covers(day) and pattern_works(self.pattern, self.anchor_date, day)


@dataclass(frozen=True)
class ShiftOverride:
    """Исключение на дату (``ShiftException``): работает или нет вопреки шаблону."""

    day: date
    is_working: bool


@dataclass(frozen=True)
class Slot:
    """Слот сетки инструкторов: id и время — чтобы знать, какие слоты закрывает группа."""

    id: int
    start: time
    end: time


@dataclass(frozen=True)
class Duty:
    """Постоянная обязанность в слоте (``InstructorDuty``).

    ``label`` — подпись для показа: название группы для ``GROUP_LEAD``, текст для ``OTHER``.
    ``span`` — время занятия группы (начало, конец) для ``GROUP_LEAD``: обязанность занимает
    все слоты, с которыми оно пересекается, а не только выбранный.
    """

    slot_id: int
    kind: DutyKind
    valid_from: date
    valid_to: date | None = None
    label: str = ""
    span: tuple[time, time] | None = None


@dataclass(frozen=True)
class Block:
    """Разовое изменение на дату (``InstructorBlock``); ``CLEAR`` — снять распорядок.

    ``span`` — как у ``Duty``: время занятия группы для разового ведения группы.
    """

    day: date
    slot_id: int
    kind: DutyKind
    label: str = ""
    span: tuple[time, time] | None = None


@dataclass(frozen=True)
class Busy:
    """Слот занят не пациентом: вид и подпись (как в ячейке шахматки)."""

    kind: DutyKind
    label: str = ""
    # Разовый блок на дату, а не постоянный распорядок.
    one_off: bool = False

    @property
    def text(self) -> str:
        """Подпись ячейки: «Эрго общая», «Метод. работа», «БОС» или текст «прочего»."""
        return self.label or DUTY_TEXT[self.kind]


@dataclass(frozen=True)
class InstructorDay:
    """Инструктор в конкретную дату: работает ли и какие слоты заняты не пациентами."""

    day: date
    working: bool
    busy: dict[int, Busy] = field(default_factory=dict)

    def is_free(self, slot_id: int) -> bool:
        """Можно ли поставить пациента: инструктор работает и в слоте нет обязанности."""
        return self.working and slot_id not in self.busy

    def free_slots(self, slot_ids: Iterable[int]) -> list[int]:
        """Свободные слоты из перечисленных, в том же порядке."""
        return [slot_id for slot_id in slot_ids if self.is_free(slot_id)]


def is_working(
    patterns: Iterable[ShiftRule], exceptions: Iterable[ShiftOverride], day: date
) -> bool:
    """Работает ли инструктор в дату (FR-STF-2, FR-STF-3). Исключение важнее шаблона."""
    for item in exceptions:
        if item.day == day:
            return item.is_working
    return any(rule.works(day) for rule in patterns)


def covered_slots(
    slot_id: int, span: tuple[time, time] | None, slots: Iterable[Slot]
) -> frozenset[int]:
    """Слоты, которые занимает обязанность: свой слот и все, с которыми пересекается ``span``.

    Касание границ (группа 9:40–10:10 и слот 9:10–9:40) пересечением не считается.
    """
    if span is None:
        return frozenset({slot_id})
    start, end = span
    return frozenset({slot_id}) | {
        slot.id for slot in slots if slot.start < end and start < slot.end
    }


def busy_slots(
    duties: Iterable[Duty],
    blocks: Iterable[Block],
    day: date,
    working: bool = True,
    slots: Iterable[Slot] = (),
) -> dict[int, Busy]:
    """Занятость слотов в дату: распорядок периода, поверх — разовые блоки (FR-STF-5).

    Распорядок действует только в рабочие дни (``working``); в нерабочий день ничего не занято.
    ``slots`` — сетка со временем: по ней ведение группы занимает все пересекающиеся слоты.
    Разовый блок снимает целиком каждую обязанность, которая занимает хотя бы один его слот
    (группу нельзя вести «наполовину»), и, кроме ``CLEAR``, занимает свои слоты сам.
    """
    if not working:
        return {}
    grid = tuple(slots)
    active = [
        (covered_slots(duty.slot_id, duty.span, grid), Busy(duty.kind, duty.label))
        for duty in duties
        if in_period(day, duty.valid_from, duty.valid_to)
    ]
    placed: list[tuple[frozenset[int], Busy]] = []
    for block in blocks:
        if block.day != day:
            continue
        covered = covered_slots(block.slot_id, block.span, grid)
        active = [(ids, busy) for ids, busy in active if not ids & covered]
        if block.kind is not DutyKind.CLEAR:
            placed.append((covered, Busy(block.kind, block.label, one_off=True)))
    result: dict[int, Busy] = {}
    for ids, busy in active + placed:
        for slot_id in ids:
            result[slot_id] = busy
    return result


@dataclass(frozen=True)
class InstructorCalendar:
    """Всё о сменах и распорядке одного инструктора — для вопросов «в дату».

    ``start…end`` — даты, на которые загружены исключения и блоки. Вопрос о дате вне диапазона —
    ``ValueError``: иначе забытая загрузка выглядела бы как «не работает» или «свободен».
    Пустой диапазон (None) — без проверки, для календарей, собранных вручную.
    """

    patterns: tuple[ShiftRule, ...] = ()
    exceptions: tuple[ShiftOverride, ...] = ()
    duties: tuple[Duty, ...] = ()
    blocks: tuple[Block, ...] = ()
    slots: tuple[Slot, ...] = ()
    start: date | None = None
    end: date | None = None

    def _check(self, day: date) -> None:
        if (self.start is not None and day < self.start) or (
            self.end is not None and day > self.end
        ):
            raise ValueError(
                f"Календарь загружен на {self.start}…{self.end}, а запрошена дата {day}."
            )

    def is_working(self, day: date) -> bool:
        self._check(day)
        return is_working(self.patterns, self.exceptions, day)

    def by_pattern(self, day: date) -> bool:
        """Рабочий ли день по шаблону — без исключений (для экрана «Смены»)."""
        return any(rule.works(day) for rule in self.patterns)

    def exception_on(self, day: date) -> ShiftOverride | None:
        self._check(day)
        return next((item for item in self.exceptions if item.day == day), None)

    def pattern_on(self, day: date) -> ShiftRule | None:
        """Шаблон, действующий в дату, или None."""
        return next((rule for rule in self.patterns if rule.covers(day)), None)

    def day(self, day: date) -> InstructorDay:
        working = self.is_working(day)
        return InstructorDay(
            day, working, busy_slots(self.duties, self.blocks, day, working, self.slots)
        )


# --- Команды шахматки ----------------------------------------------------------------------


@dataclass(frozen=True)
class TeamMember:
    id: int
    short_name: str
    display_order: int = 0


@dataclass(frozen=True)
class Team:
    """Колонка шахматки: одиночный инструктор или пара 2/2 (FR-STF-1, §7.3 «команда»).

    ``members`` упорядочены как в подписи: по порядку шахматки, затем по фамилии.
    ``key`` — стабильный id: номера инструкторов по возрастанию, «7» или «3-8».
    """

    members: tuple[TeamMember, ...]

    @classmethod
    def of(cls, *members: TeamMember) -> "Team":
        if not 1 <= len(members) <= 2:
            raise ValueError("В команде один инструктор или пара.")
        return cls(tuple(sorted(members, key=lambda m: (m.display_order, m.short_name))))

    @property
    def key(self) -> str:
        return "-".join(str(pk) for pk in sorted(m.id for m in self.members))

    @property
    def label(self) -> str:
        """«Паршуков/ Ким» для пары, иначе фамилия — как ``Instructor.team_label``."""
        return "/ ".join(m.short_name for m in self.members)

    @property
    def member_ids(self) -> tuple[int, ...]:
        return tuple(m.id for m in self.members)


def team_candidates(
    team: Team, calendars: dict[int, InstructorCalendar], day: date, slot_id: int
) -> list[int]:
    """Кто из команды работает в дату и свободен в слоте — id инструкторов в порядке команды.

    Пусто — слот команды недоступен; у пары 2/2 обычно один кандидат, но если оба вышли
    (подмена) — оба, выбор за подбором. Инструктор без календаря кандидатом не считается.
    """
    return [
        pk for pk in team.member_ids if pk in calendars and calendars[pk].day(day).is_free(slot_id)
    ]
