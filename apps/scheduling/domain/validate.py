"""Единый валидатор занятия (TZ.md §7.2, FR-SCH-5) — чистый Python без Django.

Ручная правка (шахматка, страница программы) проверяет занятие здесь до записи. Движок
подбора нарушений не создаёт: он сам выбирает только свободные варианты (§7.2, последний пункт).

Вход — кандидат (что и когда ставим) и контекст (что уже есть у пациента, инструктора и
тренажёра в эту дату). Выход — список нарушений; пустой — можно сохранять.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .engine import hhmm
from .model import ADJACENT_GAP


class ViolationCode(StrEnum):
    PATIENT_OVERLAP = "PATIENT_OVERLAP"
    INSTRUCTOR_OFF_SHIFT = "INSTRUCTOR_OFF_SHIFT"
    INSTRUCTOR_BUSY = "INSTRUCTOR_BUSY"
    EQUIPMENT_WINDOW = "EQUIPMENT_WINDOW"
    EQUIPMENT_FULL = "EQUIPMENT_FULL"
    INDIVIDUAL_LIMIT = "INDIVIDUAL_LIMIT"
    INDIVIDUAL_ADJACENT = "INDIVIDUAL_ADJACENT"
    OUT_OF_COURSE = "OUT_OF_COURSE"


class Severity(StrEnum):
    """Неустранимое — сохранить нельзя никому; с пометкой — нужен текст пометки; с
    подтверждением — нужна причина (§7.2)."""

    BLOCKING = "blocking"
    NOTE = "note"
    CONFIRM = "confirm"


SEVERITY = {
    ViolationCode.PATIENT_OVERLAP: Severity.BLOCKING,
    ViolationCode.INSTRUCTOR_BUSY: Severity.BLOCKING,
    ViolationCode.INDIVIDUAL_ADJACENT: Severity.BLOCKING,
    ViolationCode.EQUIPMENT_FULL: Severity.NOTE,
    ViolationCode.INSTRUCTOR_OFF_SHIFT: Severity.CONFIRM,
    ViolationCode.EQUIPMENT_WINDOW: Severity.CONFIRM,
    ViolationCode.INDIVIDUAL_LIMIT: Severity.CONFIRM,
    ViolationCode.OUT_OF_COURSE: Severity.CONFIRM,
}


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    message: str

    @property
    def severity(self) -> Severity:
        return SEVERITY[self.code]


@dataclass(frozen=True)
class Taken:
    """Занятие пациента в ту же дату (кроме того, которое переносим). Время — минуты."""

    start: int
    end: int
    label: str
    individual: bool = False


@dataclass(frozen=True)
class Candidate:
    """Что ставим: интервал в минутах от полуночи и вид занятия."""

    date: date
    start: int
    end: int
    individual: bool = False
    equipment: bool = False


@dataclass(frozen=True)
class Context:
    """Обстановка в дату кандидата.

    ``instructor_working`` / ``instructor_busy`` / ``instructor_patient`` — только для
    индивидуального: работает ли инструктор, чем занят слот не пациентом (подпись
    обязанности) и кто из пациентов уже стоит в слоте. ``individual_limit`` — сколько
    индивидуальных можно в день: меньшее из «р/д» назначения и лимита отделения.
    ``equipment_starts`` — шаги окна тренажёра, ``equipment_taken`` — сколько уже записано на это
    время, ``equipment_capacity`` — вместимость. ``in_course`` — дата в курсе и в датах назначения.
    """

    patient: tuple[Taken, ...] = ()
    in_course: bool = True
    instructor_working: bool = True
    instructor_busy: str = ""
    instructor_patient: str = ""
    individual_limit: int = 2
    equipment_starts: tuple[int, ...] = ()
    equipment_taken: int = 0
    equipment_capacity: int = 1


def validate(candidate: Candidate, context: Context) -> list[Violation]:
    """Все нарушения кандидата по порядку §7.2. Пустой список — можно сохранять."""
    result: list[Violation] = []
    if not context.in_course:
        result.append(
            Violation(
                ViolationCode.OUT_OF_COURSE,
                f"{candidate.date:%d.%m} — вне курса или вне дат назначения.",
            )
        )
    for item in _overlaps(candidate, context.patient):
        result.append(
            Violation(
                ViolationCode.PATIENT_OVERLAP,
                f"У пациента в это время уже {item.label} ({hhmm(item.start)}–{hhmm(item.end)}).",
            )
        )
    if candidate.individual:
        result += _individual(candidate, context)
    if candidate.equipment:
        result += _equipment(candidate, context)
    return result


def _overlaps(candidate: Candidate, taken: Iterable[Taken]) -> list[Taken]:
    return [t for t in taken if t.start < candidate.end and candidate.start < t.end]


def _individual(candidate: Candidate, context: Context) -> list[Violation]:
    result: list[Violation] = []
    if not context.instructor_working:
        result.append(
            Violation(ViolationCode.INSTRUCTOR_OFF_SHIFT, "Инструктор в этот день не работает.")
        )
    if context.instructor_busy:
        result.append(
            Violation(
                ViolationCode.INSTRUCTOR_BUSY,
                f"У инструктора в этом слоте {context.instructor_busy}.",
            )
        )
    if context.instructor_patient:
        result.append(
            Violation(
                ViolationCode.INSTRUCTOR_BUSY,
                f"У инструктора в этом слоте уже пациент {context.instructor_patient}.",
            )
        )
    own = [t for t in context.patient if t.individual]
    if len(own) + 1 > context.individual_limit:
        result.append(
            Violation(
                ViolationCode.INDIVIDUAL_LIMIT,
                f"Индивидуальных в этот день станет {len(own) + 1}, "
                f"а можно {context.individual_limit}.",
            )
        )
    for item in own:
        gap = max(item.start - candidate.end, candidate.start - item.end)
        if 0 <= gap <= ADJACENT_GAP:
            result.append(
                Violation(
                    ViolationCode.INDIVIDUAL_ADJACENT,
                    f"Два индивидуальных подряд: уже есть в {hhmm(item.start)}.",
                )
            )
    return result


def _equipment(candidate: Candidate, context: Context) -> list[Violation]:
    result: list[Violation] = []
    if candidate.start not in context.equipment_starts:
        result.append(
            Violation(
                ViolationCode.EQUIPMENT_WINDOW,
                f"{hhmm(candidate.start)} — вне окна тренажёра или не на шаге.",
            )
        )
    if context.equipment_taken >= context.equipment_capacity:
        result.append(
            Violation(
                ViolationCode.EQUIPMENT_FULL,
                f"На тренажёре в это время уже {context.equipment_taken} "
                f"при вместимости {context.equipment_capacity}.",
            )
        )
    return result


def blocking(violations: Iterable[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity is Severity.BLOCKING]
