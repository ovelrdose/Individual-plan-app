"""Входы и выходы движка. Всё неизменяемое: одинаковый снимок → одинаковое предложение."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from apps.staff.domain import Team


def is_weekend(day: date) -> bool:
    """Суббота и воскресенье. Индивидуальные в эти дни ведут дежурные 2/2 в то же время, что
    и в будни: инструктор не подбирается, нагрузка не считается (решение 56)."""
    return day.weekday() >= 5


class Kind(StrEnum):
    LFK_GROUP = "LFK_GROUP"
    POOL = "POOL"
    INDIVIDUAL = "INDIVIDUAL"
    EQUIPMENT = "EQUIPMENT"


@dataclass(frozen=True)
class Session:
    """Ежедневное занятие группы ЛФК или бассейна."""

    id: int
    start: int
    end: int


@dataclass(frozen=True)
class Need:
    """Назначение, которое нужно разложить по дням курса.

    Для групп и бассейна — sessions; для тренажёра — equipment_id, starts, duration,
    capacity; для индивидуального — только даты и частота, инструкторы и слоты — в
    ``Snapshot.staff``. per_day — сколько раз в день (у групп — каждая группа столько раз).
    dates — дни, когда назначение действует (с учётом начала и отмены).
    """

    prescription_id: int
    procedure_id: int
    kind: Kind
    label: str
    dates: tuple[date, ...]
    sessions: tuple[Session, ...] = ()
    equipment_id: int | None = None
    starts: tuple[int, ...] = ()
    duration: int = 0
    capacity: int = 1
    per_day: int = 1
    # «Бассейн» без группы: группу должен выбрать специалист ФР.
    group_required: bool = False
    # Почему назначение нельзя поставить вообще (например, тренажёр выключен в справочнике).
    unavailable: str = ""
    # Сколько занятий в день уже закреплено вручную — подбор добирает только остальное.
    pinned_units: tuple[tuple[date, int], ...] = ()

    def pinned_on(self, day: date) -> int:
        return dict(self.pinned_units).get(day, 0)


@dataclass(frozen=True)
class Busy:
    """Занятость пациента, которую движок не трогает (закреплённые бронирования).

    ``individual`` — закреплённое индивидуальное занятие: оно входит в лимит дня и с ним
    нельзя ставить другое индивидуальное подряд (INDIVIDUAL_ADJACENT).
    """

    date: date
    start: int
    end: int
    individual: bool = False


@dataclass(frozen=True)
class EquipmentLoad:
    """Запись на тренажёр другого пациента — занимает место в пределах вместимости."""

    equipment_id: int
    date: date
    start: int
    end: int


@dataclass(frozen=True)
class Slot:
    """Слот сетки инструкторов (FR-CAT-5). Вечерние подбор не использует (решение 10)."""

    id: int
    start: int
    end: int
    evening: bool = False


@dataclass(frozen=True)
class Assignment:
    """Индивидуальное занятие, поставленное прошлым подбором (решение 49)."""

    prescription_id: int
    date: date
    instructor_id: int
    slot_id: int


@dataclass(frozen=True)
class Staff:
    """Инструкторы для индивидуальных занятий (TZ.md §7.3, шаг 3).

    ``free`` — тройки (дата, слот, инструктор), где инструктор работает, слот не занят
    распорядком, разовым блоком и индивидуальными занятиями других программ всех отделений.
    ``working`` — кто работает по графику в даты курса (для средней загрузки и штатной подмены).
    ``load`` — (инструктор, число его индивидуальных занятий в других программах за даты курса).
    ``previous`` — прошлые автоматические постановки этой программы: они сохраняются первыми,
    если ещё допустимы (решение 49). ``preferred`` — инструктор по желанию пациента (решение 44).
    """

    slots: tuple[Slot, ...] = ()
    teams: tuple[Team, ...] = ()
    free: frozenset[tuple[date, int, int]] = frozenset()
    # (дата, инструктор) — работает по графику в дату курса. Пусто — считается по ``free``.
    working: frozenset[tuple[date, int]] = frozenset()
    load: tuple[tuple[int, int], ...] = ()
    preferred: int | None = None
    # Фамилия для сообщений: инструктор по желанию мог уже не работать (нет среди команд).
    preferred_name: str = ""
    previous: tuple[Assignment, ...] = ()
    max_per_day: int = 2


@dataclass(frozen=True)
class Snapshot:
    needs: tuple[Need, ...]
    patient_busy: tuple[Busy, ...] = ()
    equipment_load: tuple[EquipmentLoad, ...] = ()
    staff: Staff = Staff()


@dataclass(frozen=True)
class Placement:
    prescription_id: int
    procedure_id: int
    kind: Kind
    date: date
    start: int
    end: int
    session_id: int | None = None
    equipment_id: int | None = None
    instructor_id: int | None = None
    slot_id: int | None = None


class IssueCode(StrEnum):
    # Конфликты — занятие не поставлено, нужен человек.
    GROUP_OVERLAP = "GROUP_OVERLAP"
    GROUP_FREQUENCY = "GROUP_FREQUENCY"
    POOL_TYPE_REQUIRED = "POOL_TYPE_REQUIRED"
    NO_SESSIONS = "NO_SESSIONS"
    EQUIPMENT_UNPLACED = "EQUIPMENT_UNPLACED"
    INDIVIDUAL_UNPLACED = "INDIVIDUAL_UNPLACED"
    # Предупреждения — поставлено, но не так, как хотелось бы.
    DAY_DEVIATION = "DAY_DEVIATION"
    PREFERRED_REPLACED = "PREFERRED_REPLACED"
    INDIVIDUAL_LIMIT = "INDIVIDUAL_LIMIT"


CONFLICTS = {
    IssueCode.GROUP_OVERLAP,
    IssueCode.GROUP_FREQUENCY,
    IssueCode.POOL_TYPE_REQUIRED,
    IssueCode.NO_SESSIONS,
    IssueCode.EQUIPMENT_UNPLACED,
    IssueCode.INDIVIDUAL_UNPLACED,
}


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    prescription_id: int
    message: str
    date: date | None = None

    @property
    def is_conflict(self) -> bool:
        return self.code in CONFLICTS


@dataclass
class Proposal:
    placements: list[Placement] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def conflicts(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_conflict]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if not issue.is_conflict]
