"""Журнал расписания (FR-SCH-19, решение 86): индивидуальные и тренажёры — где пациент был и
где стал, кто или что это сделало и почему.

Записи не пишутся по месту каждого изменения: мест много (подбор, шахматка дня, ручная правка,
сдвиг тренажёра ради индивидуального). Вместо этого действие открывает ``action`` — кто и что
делает, — а всё, что меняет пациента, сначала зовёт ``watch``: запоминается его состояние до
изменения. Когда внешнее действие заканчивается, состояние сравнивается с тем, что стало,
и в журнал идут только настоящие изменения (``domain.journal.changes``). Пересчёт, после
которого всё осталось на месте, записей не даёт.

Причины решений системы приносят расчёты: ``explain`` — пояснения шахматки дня
(``board_day.PatientDay.why``) и подбора тренажёров (``Proposal.reasons``).

Вложенные действия: пациент относится к тому действию, в котором его впервые тронули. Так
правка специалиста записывается от его имени, а ждущий, вставший на освободившееся место
(``refill_equipment``), — от имени системы.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import wraps
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone

from apps.accounts.access import departments_with, has_right
from apps.accounts.models import Right, User
from apps.catalog.models import ProcedureKind
from apps.programs.models import Prescription, Program

from .domain.journal import INDIVIDUAL, Change, Key, changes
from .models import (
    BoardDay,
    BoardPatient,
    Booking,
    BookingKind,
    EventSource,
    RemovedKind,
    RemovedSession,
    ScheduleEvent,
)

KEEP_DAYS = 7
INDIVIDUAL_TITLE = "Индивидуальные"
UNPLACED = "Не распределены"
CANCELLED = "Отменены"


@dataclass
class _Frame:
    action: str
    user: User | None = None


@dataclass
class _Session:
    frames: list[_Frame]
    before: dict[int, dict[Key, str]] = field(default_factory=dict)
    cause: dict[int, _Frame] = field(default_factory=dict)
    reasons: dict[int, dict[Key, list[str]]] = field(default_factory=lambda: defaultdict(dict))
    notes: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    base: dict[date, date] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


_session: ContextVar[_Session | None] = ContextVar("schedule_journal", default=None)


@contextmanager
def action(text: str, user: User | None = None, *, fallback: bool = False) -> Iterator[None]:
    """Действие с расписанием: ``user`` — кто (нет — система), ``text`` — что делает.

    ``fallback`` — только если действия ещё нет: пересборка внутри правки пользователя
    записывается от его имени, а не как «пересборка системой»."""
    frame = _Frame(text, user if user is not None and user.pk else None)
    session = _session.get()
    # Внутри действия пользователя его же вложенные шаги («не работает» → «смена изменилась»)
    # называются внешним действием — тем, что он сделал на экране.
    absorbed = (
        frame.user is not None and any(f.user is not None for f in session.frames)
        if session
        else False
    )
    if session is not None and (fallback or absorbed):
        yield
        return
    if session is not None:
        session.frames.append(frame)
        try:
            yield
        finally:
            session.frames.pop()
        return
    session = _Session([frame])
    token = _session.set(session)
    try:
        yield
        _flush(session)
    finally:
        _session.reset(token)


def logged(text: str) -> Callable:
    """Сервис, первый аргумент которого — пользователь: всё, что он изменит, — от его имени.
    Ставить под ``@transaction.atomic``: запись журнала — в той же транзакции."""

    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def wrapper(user: User, *args: Any, **kwargs: Any) -> Any:
            with action(text, user):
                return function(user, *args, **kwargs)

        return wrapper

    return decorate


def watch(programs: Iterable[Program | int]) -> None:
    """Запомнить состояние пациентов до изменения — звать до того, как их трогать."""
    session = _session.get()
    if session is None:
        return
    ids = sorted({p if isinstance(p, int) else p.pk for p in programs} - set(session.before))
    if not ids:
        return
    states = _states(ids, session)
    for pk in ids:
        session.before[pk] = states.get(pk, {})
        session.cause[pk] = session.frames[-1]


def explain(program_id: int, subject: str, day: date, text: str) -> None:
    """Почему пациент в этот день встал так или не встал — пояснение расчёта."""
    session = _session.get()
    if session is None or not text:
        return
    items = session.reasons[program_id].setdefault((subject, day), [])
    if text not in items:
        items.append(text)


def note(program_id: int, text: str) -> None:
    """Пояснение ко всем изменениям пациента в этом действии — например, причина специалиста."""
    session = _session.get()
    if session is not None and text and text not in session.notes[program_id]:
        session.notes[program_id].append(text)


def new_board(day: date, source: date | None) -> None:
    """Составлена шахматка ``day`` из ``source``: индивидуальные нового дня сравниваются с
    ``source`` — перенос «как вчера» изменением не считается."""
    session = _session.get()
    if session is None or source is None:
        return
    session.base[day] = source
    # Пациенты, которых тронули раньше, — дочитать их состояние на ``source``.
    if session.before:
        extra = _states(list(session.before), session, individual_dates=[source], equipment=False)
        for pk, state in extra.items():
            for key, value in state.items():
                session.before[pk].setdefault(key, value)


def equipment_subject(equipment_id: int) -> str:
    return f"eq:{equipment_id}"


def purge(now: Any = None) -> int:
    """Журнал ведётся неделю (решение 86)."""
    edge = (now or timezone.now()) - timedelta(days=KEEP_DAYS)
    deleted, _ = ScheduleEvent.objects.filter(created_at__lt=edge).delete()
    return deleted


# --- Состояние пациента ---------------------------------------------------------------------


def _states(
    ids: list[int],
    session: _Session,
    *,
    individual_dates: list[date] | None = None,
    equipment: bool = True,
) -> dict[int, dict[Key, str]]:
    """Состояние пациентов: индивидуальные в составленных шахматках с сегодняшнего дня (и в
    днях, с которых составлялись новые) и тренажёры с сегодняшнего дня до конца курса."""
    from . import board_days  # board_days пишет журнал сам — импорт по месту

    today = board_days.today()
    if individual_dates is None:
        individual_dates = sorted(
            set(BoardDay.objects.filter(date__gte=today).values_list("date", flat=True))
            | set(session.base.values())
        )
    result: dict[int, dict[Key, str]] = defaultdict(dict)
    _individual(ids, individual_dates, result)
    if equipment:
        _equipment(ids, today, result, session.names)
    return result


def _individual(ids: list[int], dates: list[date], result: dict[int, dict[Key, str]]) -> None:
    parts: dict[tuple[int, date], list[str]] = defaultdict(list)
    for booking in (
        Booking.objects.filter(program__in=ids, kind=BookingKind.INDIVIDUAL, date__in=dates)
        .select_related("instructor")
        .order_by("start")
    ):
        text = (
            f"{booking.instructor.short_name if booking.instructor else '?'} {booking.start:%-H:%M}"
        )
        if booking.note:
            text += f" ({booking.note})"
        parts[(booking.program_id, booking.date)].append(text)
    for entry in BoardPatient.objects.filter(program__in=ids, date__in=dates):
        if entry.unplaced:
            parts[(entry.program_id, entry.date)].append(_times(UNPLACED, entry.unplaced))
        if entry.cancelled:
            parts[(entry.program_id, entry.date)].append(_times(CANCELLED, entry.cancelled))
    for (pk, day), items in parts.items():
        result[pk][(INDIVIDUAL, day)] = ", ".join(items)


def _equipment(
    ids: list[int], today: date, result: dict[int, dict[Key, str]], names: dict[str, str]
) -> None:
    prescriptions = list(
        Prescription.objects.filter(
            program__in=ids,
            procedure__kind=ProcedureKind.EQUIPMENT,
            procedure__equipment__isnull=False,
        )
        .select_related("procedure__equipment", "program")
        .prefetch_related("program__withdrawals")
    )
    times: dict[tuple[int, date], list[str]] = defaultdict(list)
    for prescription_id, day, start in (
        Booking.objects.filter(prescription__in=prescriptions, date__gte=today)
        .order_by("start")
        .values_list("prescription_id", "date", "start")
    ):
        times[(prescription_id, day)].append(f"{start:%-H:%M}")
    removed: dict[tuple[int, date], dict[str, int]] = defaultdict(dict)
    for item in RemovedSession.objects.filter(prescription__in=prescriptions, date__gte=today):
        removed[(item.prescription_id, item.date)][item.kind] = item.units
    parts: dict[tuple[int, str, date], list[str]] = defaultdict(list)
    for p in prescriptions:
        subject = equipment_subject(p.procedure.equipment_id)
        names[subject] = p.procedure.equipment.name
        program = p.program
        days = {d for d in program.course_dates() if d >= today and _active(p, program, d)}
        days |= {d for (pk, d) in times if pk == p.pk}
        for day in sorted(days):
            placed = times.get((p.pk, day), [])
            kinds = removed.get((p.pk, day), {})
            items = list(placed)
            if kinds.get(RemovedKind.HELD):
                items.append(_times("Не распределён (снят вручную)", kinds[RemovedKind.HELD]))
            if kinds.get(RemovedKind.CANCELLED):
                items.append(_times("Отменён", kinds[RemovedKind.CANCELLED]))
            if kinds.get(RemovedKind.REMOVED):
                items.append(_times("Убран на странице программы", kinds[RemovedKind.REMOVED]))
            missing = p.per_day - len(placed) - sum(kinds.values())
            if missing > 0 and _active(p, program, day):
                items.append(_times("Не распределён", missing))
            parts[(program.pk, subject, day)] += items
    for (pk, subject, day), items in parts.items():
        result[pk][(subject, day)] = ", ".join(items)


def _active(prescription: Prescription, program: Program, day: date) -> bool:
    start = prescription.start_date or program.start_date
    return (
        program.is_therapy_day(day)
        and start <= day
        and (prescription.cancel_date is None or day < prescription.cancel_date)
    )


def _times(text: str, units: int) -> str:
    return text if units == 1 else f"{text} ×{units}"


# --- Запись ---------------------------------------------------------------------------------


def _flush(session: _Session) -> None:
    alive = set(Program.objects.filter(pk__in=list(session.before)).values_list("pk", flat=True))
    # В порядке, обратном первому касанию: журнал читается сверху вниз от новых, и пациент,
    # которого правили, оказывается выше тех, кого это задело (ждущий встал на его место).
    ids = [pk for pk in reversed(session.before) if pk in alive]
    if not ids:
        return
    after = _states(ids, session)
    events: list[ScheduleEvent] = []
    for pk in ids:
        frame = session.cause[pk]
        found = changes(
            session.before[pk], after.get(pk, {}), session.reasons.get(pk), session.base
        )
        for change in found:
            events.append(_event(pk, change, frame, session))
    if events:
        ScheduleEvent.objects.bulk_create(events)


def _event(program_id: int, change: Change, frame: _Frame, session: _Session) -> ScheduleEvent:
    equipment_id = None
    if change.subject.startswith("eq:"):
        equipment_id = int(change.subject[3:])
    reason = "; ".join(filter(None, [change.reason, *session.notes.get(program_id, [])]))
    return ScheduleEvent(
        program_id=program_id,
        subject=INDIVIDUAL_TITLE
        if equipment_id is None
        else session.names.get(change.subject, "тренажёр"),
        equipment_id=equipment_id,
        date_from=change.date_from,
        date_to=change.date_to,
        before=change.before[:300],
        after=change.after[:300],
        source=EventSource.MANUAL if frame.user is not None else EventSource.SYSTEM,
        user=frame.user,
        action=frame.action[:300],
        reason=reason,
    )


# --- Чтение ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """Фильтры экрана журнала: дата расписания, пациент (палата или фамилия), что (``ind`` —
    индивидуальные, ``eq:<id>`` — тренажёр), кто (система / вручную), программа."""

    day: date | None = None
    patient: str = ""
    subject: str = ""
    source: str = ""
    program_id: int | None = None


def entries(user: User, filters: Filters) -> Any:
    """Записи журнала за неделю по пациентам отделений, где пользователь правит расписание
    (решение 86). Без права — ошибка доступа."""
    departments = departments_with(user, Right.SCHEDULE)
    if not departments.exists():
        raise PermissionDenied("Журнал расписания — для тех, кто правит расписание пациентов.")
    items = ScheduleEvent.objects.filter(program__department__in=departments).select_related(
        "program", "user"
    )
    if filters.day is not None:
        items = items.filter(date_from__lte=filters.day, date_to__gte=filters.day)
    if filters.patient.strip():
        text = filters.patient.strip()
        items = items.filter(Q(program__full_name__icontains=text) | Q(program__room__iexact=text))
    if filters.subject == INDIVIDUAL:
        items = items.filter(equipment__isnull=True)
    elif filters.subject.startswith("eq:") and filters.subject[3:].isdigit():
        items = items.filter(equipment_id=int(filters.subject[3:]))
    if filters.source in EventSource.values:
        items = items.filter(source=filters.source)
    if filters.program_id is not None:
        items = items.filter(program_id=filters.program_id)
    return items


def history(user: User, program: Program, limit: int = 8) -> list[ScheduleEvent]:
    """Последние записи журнала пациента — для панели в шахматке."""
    if not has_right(user, Right.SCHEDULE, program.department):
        return []
    return list(ScheduleEvent.objects.filter(program=program).select_related("user")[:limit])
