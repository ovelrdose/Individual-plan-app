"""Входы и выходы движка. Всё неизменяемое: одинаковый снимок → одинаковое предложение."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

# Два индивидуальных занятия с перерывом не больше 10 минут — «подряд» (TZ.md FR-SCH-7).
ADJACENT_GAP = 10


def is_weekend(day: date) -> bool:
    """Суббота и воскресенье: шахматки индивидуальных в эти дни нет (TZ.md §7)."""
    return day.weekday() >= 5


class Kind(StrEnum):
    LFK_GROUP = "LFK_GROUP"
    DS_GROUP = "DS_GROUP"
    POOL = "POOL"
    EQUIPMENT = "EQUIPMENT"


@dataclass(frozen=True)
class Session:
    """Ежедневное занятие группы ЛФК, ДС или бассейна."""

    id: int
    start: int
    end: int


@dataclass(frozen=True)
class Need:
    """Назначение, которое нужно разложить по дням курса.

    Для групп и бассейна — sessions; для тренажёра — equipment_id, starts, duration,
    capacity. per_day — сколько раз в день (у групп — каждая группа столько раз).
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
    # Время группы, выбранное на странице программы или взятое из листа (решение 81): основное
    # время первого занятия дня, без сравнения с другими.
    chosen_session_id: int | None = None
    # Занятий у группы в день больше, чем назначено, а время не выбрано: подбор ставит сам
    # и предупреждает (GROUP_TIME_AUTO).
    choice_needed: bool = False
    # На каких занятиях стояли автоматические до пересборки, частые — первыми: при равном
    # выборе время не прыгает (например, когда у группы добавили более раннее занятие).
    previous_session_ids: tuple[int, ...] = ()

    def pinned_on(self, day: date) -> int:
        return dict(self.pinned_units).get(day, 0)


@dataclass(frozen=True)
class Busy:
    """Занятость пациента, которую движок не трогает (закреплённые бронирования)."""

    date: date
    start: int
    end: int
    # Что за занятие — для пояснений журнала («занят: Эрго общая 13:00»).
    label: str = ""


@dataclass(frozen=True)
class EquipmentLoad:
    """Запись на тренажёр другого пациента — занимает место в пределах вместимости."""

    equipment_id: int
    date: date
    start: int
    end: int


@dataclass(frozen=True)
class Snapshot:
    needs: tuple[Need, ...]
    patient_busy: tuple[Busy, ...] = ()
    equipment_load: tuple[EquipmentLoad, ...] = ()
    # Индивидуальные пациента в составленных шахматках: группа без выбранного времени по
    # возможности их обходит, но не обязана (выбранное время важнее индивидуального).
    soft_busy: tuple[Busy, ...] = ()


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


class IssueCode(StrEnum):
    # Конфликты — занятие не поставлено, нужен человек.
    GROUP_OVERLAP = "GROUP_OVERLAP"
    GROUP_FREQUENCY = "GROUP_FREQUENCY"
    POOL_TYPE_REQUIRED = "POOL_TYPE_REQUIRED"
    NO_SESSIONS = "NO_SESSIONS"
    EQUIPMENT_UNPLACED = "EQUIPMENT_UNPLACED"
    # Предупреждения — поставлено, но не так, как хотелось бы.
    DAY_DEVIATION = "DAY_DEVIATION"
    # Время группы не выбрано, а занятий в день больше назначенного (решение 81).
    GROUP_TIME_AUTO = "GROUP_TIME_AUTO"


CONFLICTS = {
    IssueCode.GROUP_OVERLAP,
    IssueCode.GROUP_FREQUENCY,
    IssueCode.POOL_TYPE_REQUIRED,
    IssueCode.NO_SESSIONS,
    IssueCode.EQUIPMENT_UNPLACED,
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
    # Почему тренажёр в этот день встал так или не встал: (назначение, дата) → текст для
    # журнала расписания (FR-SCH-19).
    reasons: dict[tuple[int, date], str] = field(default_factory=dict)

    @property
    def conflicts(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_conflict]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if not issue.is_conflict]
