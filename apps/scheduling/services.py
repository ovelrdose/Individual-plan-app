"""Расписание программы (TZ.md §7): подбор, хранение, типичный день."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, ProtectedError, Q, QuerySet

from apps.accounts.access import departments_for, has_right, is_admin, require_right
from apps.accounts.models import Right, User
from apps.catalog.domain import minutes_between
from apps.catalog.initial_data import EQUIPMENT_PLACE
from apps.catalog.models import (
    SCHEDULED_KINDS,
    SESSION_KINDS,
    Equipment,
    GroupSession,
    InstructorSlot,
    Procedure,
    ProcedureKind,
    Section,
    SectionBehavior,
)
from apps.live import events as live
from apps.live import topics as live_topics
from apps.org.models import Department
from apps.programs.models import Prescription, Program, withdrawn_on
from apps.staff.models import Instructor, InstructorBlock, InstructorDuty

from . import board_days, staff_changes
from .domain import (
    Busy,
    EquipmentLoad,
    GridRow,
    Need,
    Proposal,
    ScheduledItem,
    Session,
    Snapshot,
    TypicalRow,
    day_grid,
    is_weekend,
    propose,
    typical_day,
)
from .domain.model import IssueCode, Kind
from .models import (
    GROUP_BOOKING_KINDS,
    BoardDay,
    BoardPatient,
    Booking,
    BookingKind,
    BookingSource,
    RemovedSession,
)

# Что ставит подбор на весь курс: группы ЛФК, бассейн и тренажёры (TZ.md, FR-SCH-2).
# Индивидуальные — только в шахматке на день вперёд (board_days, FR-SCH-3).
AUTO_KINDS = (
    ProcedureKind.LFK_GROUP,
    ProcedureKind.DS_GROUP,
    ProcedureKind.POOL,
    ProcedureKind.EQUIPMENT,
)
MIDNIGHT = time(0, 0)


class ScheduleError(Exception):
    """Действие с расписанием невозможно. Текст показывается пользователю."""


def can_schedule(user: User, program: Program) -> bool:
    """Расписание пациента — право «Расписание пациентов» в его отделении (решение 79)."""
    return has_right(user, Right.SCHEDULE, program.department)


def can_choose_group_time(user: User, program: Program) -> bool:
    """Время групп выбирают врач и специалист ФР (решение 81): кто правит назначения или
    расписание пациента. У выбывшего назначения не правят — остаётся право расписания."""
    if has_right(user, Right.SCHEDULE, program.department):
        return True
    return has_right(user, Right.PROGRAMS_EDIT, program.department) and program.withdrawal is None


def pool_groups() -> list[Procedure]:
    """Группы бассейна, из которых выбирает специалист ФР: действующие и с занятиями."""
    return list(
        Procedure.objects.filter(kind=ProcedureKind.POOL, is_active=True, sessions__is_active=True)
        .distinct()
        .prefetch_related("sessions")
        .order_by("name")
    )


@transaction.atomic
def choose_pool_group(user: User, prescription: Prescription, group: Procedure | None) -> None:
    """Группа бассейна для назначения «Бассейн» без группы (TZ.md, FR-PRG-3). Пустое значение
    снимает выбор. Расписание сразу пересобирается: занятия встают во время выбранной группы."""
    program = (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=prescription.program_id)
    )
    require_right(user, Right.SCHEDULE, program.department)
    prescription = (
        program.prescriptions.select_related("procedure").filter(pk=prescription.pk).first()
    )
    if prescription is None:
        raise ScheduleError("Назначение уже удалено. Обновите страницу.")
    if prescription.procedure is None or not prescription.procedure.is_generic_pool:
        raise ScheduleError("Группу бассейна выбирают только для назначения «Бассейн» без группы.")
    if group is not None and group.pk not in {item.pk for item in pool_groups()}:
        raise ScheduleError("Выберите действующую группу бассейна с занятиями в расписании.")
    prescription.pool_group = group
    # Время выбиралось из занятий прежней группы — у новой оно другое (решение 81).
    if prescription.group_session_id and not prescription.session_fits():
        prescription.group_session = None
    prescription._history_user = user
    prescription.save(update_fields=["pool_group", "group_session"])
    replan(program)


@dataclass(frozen=True)
class Displaced:
    """Индивидуальное в составленной шахматке, которое пересеклось с группой: пациент ушёл
    в «Не распределены» (FR-SCH-6, решение 81)."""

    program: Program
    date: date
    start: time
    instructor: str

    def __str__(self) -> str:
        return (
            f"{self.date:%d.%m}: индивидуальное в {self.start:%-H:%M} ({self.instructor}) "
            "пересеклось с группой — пациент в «Не распределены»"
        )


@dataclass(frozen=True)
class GroupTimeResult:
    displaced: list[Displaced] = field(default_factory=list)


@transaction.atomic
def choose_group_time(
    user: User, prescription: Prescription, session: GroupSession | None
) -> GroupTimeResult:
    """Время группы на весь курс (решение 81): занятие группы назначения; ``None`` — снова
    «авто». Выбор сохраняется, даже если время легло на индивидуальное в составленной
    шахматке: индивидуальное уходит в «Не распределены», результат называет такие занятия."""
    program = (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=prescription.program_id)
    )
    if not can_choose_group_time(user, program):
        raise PermissionDenied("Нет права выбирать время групп.")
    prescription = (
        program.prescriptions.select_related("procedure", "pool_group", "group_session")
        .filter(pk=prescription.pk)
        .first()
    )
    if prescription is None:
        raise ScheduleError("Назначение уже удалено. Обновите страницу.")
    procedure = prescription.procedure
    if procedure is None or procedure.kind not in SESSION_KINDS:
        raise ScheduleError("Время выбирают только для групп ЛФК, ДС и бассейна.")
    group = prescription.choice_group
    if group is None:
        raise ScheduleError("Сначала выберите группу бассейна.")
    if session is not None and (session.procedure_id != group.pk or not session.is_active):
        raise ScheduleError("Выберите действующее занятие этой группы.")
    if prescription.group_session_id == (session.pk if session else None):
        return GroupTimeResult()
    with watch_displaced(program) as displaced:
        prescription.group_session = session
        prescription._history_user = user
        prescription.save(update_fields=["group_session"])
        replan(program)
    return GroupTimeResult(displaced)


def reset_choices(session_ids: list[int], user: User | None) -> list[Prescription]:
    """Занятие выключили или удалили: выбор этого времени сбрасывается с записью в журнал
    (автор — пользователь), группа снова ставится автоматически (решение 81). Завершённые
    курсы (закончились или пациент выбыл) не трогаем: их расписание уже не пересобирается.
    Вызывать под блокировкой программ — до их пересборки."""
    today = board_days.today()
    current = Program.objects.filter(end_date__gte=today).exclude(withdrawn_on(today))
    items = list(
        Prescription.objects.filter(group_session__in=session_ids, program__in=current)
        .select_related("program__department", "procedure", "group_session")
        .order_by("program__room", "pk")
    )
    for item in items:
        item.group_session = None
        item._history_user = user
        item.save(update_fields=["group_session"])
    return items


@contextmanager
def watch_displaced(program: Program | None = None) -> Iterator[list[Displaced]]:
    """Какие индивидуальные ушли в «Не распределены» за время блока: снимок шахматок до и
    после (решение 81). Список заполняется при выходе из блока; ``program`` — только этот
    пациент. Общий механизм для всех действий, после которых ``replan`` может вытеснить
    индивидуальное."""
    before = _board_snapshot()
    found: list[Displaced] = []
    yield found
    found.extend(
        item for item in _displaced(before) if program is None or item.program.pk == program.pk
    )


@dataclass(frozen=True)
class _BoardSnapshot:
    seats: dict[tuple[int, date], list[Booking]]
    unplaced: dict[tuple[int, date], int]


def _board_snapshot() -> _BoardSnapshot:
    """Индивидуальные и «Не распределены» в составленных шахматках (сегодня и дальше)."""
    today = board_days.today()
    seats: dict[tuple[int, date], list[Booking]] = defaultdict(list)
    for booking in Booking.objects.filter(
        kind=BookingKind.INDIVIDUAL, date__gte=today
    ).select_related("instructor", "program__department"):
        seats[(booking.program_id, booking.date)].append(booking)
    unplaced = {
        (program_id, day): units
        for program_id, day, units in BoardPatient.objects.filter(date__gte=today).values_list(
            "program_id", "date", "unplaced"
        )
    }
    return _BoardSnapshot(seats, unplaced)


def _displaced(before: _BoardSnapshot) -> list[Displaced]:
    """Какие индивидуальные ушли в «Не распределены» после пересборки."""
    after = _board_snapshot()
    result = []
    for key, seats in sorted(before.seats.items(), key=lambda item: (item[0][1], item[0][0])):
        if after.unplaced.get(key, 0) <= before.unplaced.get(key, 0):
            continue
        # У пары 2/2 место может перейти к напарнику — сравниваем по слоту.
        kept = {booking.slot_id for booking in after.seats.get(key, [])}
        for booking in sorted(seats, key=lambda b: b.start):
            if booking.slot_id not in kept:
                result.append(
                    Displaced(
                        booking.program, booking.date, booking.start, _short(booking.instructor)
                    )
                )
    return result


def _short(instructor: Instructor | None) -> str:
    return instructor.short_name if instructor else ""


@transaction.atomic
def replan(program: Program) -> Proposal:
    """Перестраивает расписание программы: незакреплённые занятия групп, бассейна и
    тренажёров удаляются и подбираются заново по текущим назначениям, дням занятий и
    расписанию групп (FR-SCH-1, FR-SCH-2). Индивидуальные живут в шахматке на день вперёд:
    после пересборки пациент сверяется с составленными шахматками (``board_days``).

    Вызывается после импорта и после любых изменений назначений и курса — специалисту не
    нужно помнить о «пересчитать». Закреплённые вручную занятия остаются, кроме тех, что
    оказались вне курса или вне дат назначения.

    Порядок блокировок везде один: программа → тренажёры → инструкторы → шахматки.
    """
    program = (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=program.pk)
    )
    every = {
        p.pk: p for p in program.prescriptions.select_related("procedure__equipment", "pool_group")
    }
    prescriptions = [p for p in every.values() if p.procedure and p.procedure.kind in AUTO_KINDS]
    active = {pk: set(_active_dates(program, p)) for pk, p in every.items()}
    equipment_ids = sorted(
        {p.procedure.equipment_id for p in prescriptions if p.procedure.equipment_id}
    )
    # Блокируем тренажёры по порядку: два параллельных подбора не превысят вместимость
    # и не заблокируют друг друга.
    equipment = {
        e.pk: e
        for e in Equipment.objects.select_for_update().filter(pk__in=equipment_ids).order_by("pk")
    }
    board_days.ensure_boards()
    board_days.lock()

    # Индивидуальные в составленных шахматках (сегодня и дальше) на время пересборки снимаем:
    # группы идут по своему расписанию, а пациент потом сверяется с шахматкой — занятие,
    # которое пересеклось с группой, уходит в «Не распределены» (FR-SCH-6). Прошлые дни
    # не трогаем — они только для просмотра.
    lifted = program.bookings.filter(kind=BookingKind.INDIVIDUAL, date__gte=board_days.today())
    seats: dict[date, list[board_days.Seat]] = defaultdict(list)
    # Группа без выбранного времени по возможности обходит индивидуальные (решение 81).
    soft: list[Busy] = []
    for item in lifted:
        seats[item.date].append(board_days.Seat(item.instructor_id, item.slot_id, item.note))
        soft.append(Busy(item.date, _minutes(item.start), _minutes(item.end)))
    lifted._raw_delete(Booking.objects.db)

    bookings = list(program.bookings.select_related("group_session"))
    # Автоматические занятия пересоздаются целиком и без журнала (TZ.md §13, п. 30):
    # иначе каждая правка назначения писала бы в историю сотни строк.
    automatic = [b for b in bookings if not b.pinned and b.kind in AUTO_KINDS]
    # Пометки тренажёров переходят на следующие дни, как у индивидуальных (FR-SCH-10):
    # новая запись берёт пометку ближайшего прошлого дня того же назначения.
    notes = _equipment_notes(bookings)
    # Где стояли группы до пересборки — чтобы без выбора время не прыгало (решение 81).
    previous: dict[int, Counter[int]] = defaultdict(Counter)
    for booking in automatic:
        if booking.group_session_id:
            previous[booking.prescription_id][booking.group_session_id] += 1
    Booking.objects.filter(pk__in=[b.pk for b in automatic])._raw_delete(Booking.objects.db)
    # Закреплённые вручную группы и тренажёры остаются, пока подходят к назначению.
    # Выпавшие из курса или из дат назначения удаляются обычным путём, с записью в журнал.
    for booking in bookings:
        if (
            booking.kind in AUTO_KINDS
            and booking.pinned
            and not _still_valid(booking, every, active)
        ):
            booking.delete()
    pinned = list(program.bookings.all())

    removed: Counter[tuple[int, date]] = Counter()
    for item in RemovedSession.objects.filter(prescription__program=program):
        removed[(item.prescription_id, item.date)] += item.units
    needs = tuple(
        _need(p, active[p.pk], pinned, equipment, removed, previous.get(p.pk))
        for p in prescriptions
    )
    snapshot = Snapshot(
        needs=needs,
        patient_busy=tuple(Busy(b.date, _minutes(b.start), _minutes(b.end)) for b in pinned),
        equipment_load=_equipment_load(program, equipment_ids),
        soft_busy=tuple(soft),
    )
    proposal = propose(snapshot)

    Booking.objects.bulk_create(
        [
            Booking(
                program=program,
                prescription_id=placement.prescription_id,
                procedure_id=placement.procedure_id,
                kind=placement.kind,
                date=placement.date,
                start=_time(placement.start),
                end=_time(placement.end),
                group_session_id=placement.session_id,
                equipment_id=placement.equipment_id,
                note=_note_on(notes, placement.prescription_id, placement.date)
                if placement.equipment_id
                else "",
                source=BookingSource.AUTO,
            )
            for placement in proposal.placements
        ]
    )
    program.schedule_issues = [
        {"code": issue.code, "message": issue.message, "conflict": issue.is_conflict}
        for issue in proposal.issues
    ]
    program.save(update_fields=["schedule_issues", "updated_at"])
    board_days.sync_program(program, seats)
    # Пакетные записи выше сигналов не дают: тренажёры видны и в шахматках (TZ.md NFR-11).
    live.publish(
        live_topics.program(program.pk),
        *(live_topics.board(day) for day in board_days.board_dates(board_days.today())),
    )
    return proposal


def _current_equipment_notes(bookings: list[Booking], today: date) -> dict[int, str]:
    """Пометка тренажёра по назначению — ближайшего дня с сегодняшнего, как индивидуальные
    в карте из ближайшей шахматки; курс в прошлом — последнего дня."""
    chosen: dict[int, Booking] = {}
    for b in bookings:
        if b.kind != BookingKind.EQUIPMENT:
            continue
        current = chosen.get(b.prescription_id)
        if current is None or _closer(b.date, current.date, today):
            chosen[b.prescription_id] = b
    return {pk: b.note for pk, b in chosen.items()}


def _closer(day: date, other: date, today: date) -> bool:
    if (day >= today) != (other >= today):
        return day >= today
    return day < other if day >= today else day > other


def _equipment_notes(bookings: list[Booking]) -> dict[int, list[tuple[date, str]]]:
    """Пометки записей на тренажёр по назначениям: [(дата, пометка)] по датам."""
    result: dict[int, dict[date, str]] = defaultdict(dict)
    for booking in sorted(bookings, key=lambda b: (b.date, b.start)):
        if booking.kind == BookingKind.EQUIPMENT and booking.prescription_id:
            result[booking.prescription_id][booking.date] = booking.note
    return {pk: sorted(days.items()) for pk, days in result.items()}


def _note_on(notes: dict[int, list[tuple[date, str]]], prescription_id: int, day: date) -> str:
    """Пометка ближайшего дня не позже ``day``; пустая пометка тоже переходит — её сняли."""
    note = ""
    for item_day, item_note in notes.get(prescription_id, ()):
        if item_day > day:
            break
        note = item_note
    return note


def _still_valid(
    booking: Booking, prescriptions: dict[int, Prescription], active: dict[int, set[date]]
) -> bool:
    prescription = prescriptions.get(booking.prescription_id)
    if prescription is None or prescription.procedure is None:
        return False
    if booking.date not in active[prescription.pk]:
        return False
    session = booking.group_session
    if session is not None and (
        not session.is_active
        or (booking.start, booking.end) != (session.start_time, session.end_time)
    ):
        # Занятие группы выключили или перенесли: закреплённое в старое время снимается,
        # группа встаёт по актуальному расписанию групп (решение 81).
        return False
    if booking.procedure_id == prescription.procedure_id:
        return True
    # «Бассейн» без группы: занятие стоит в выбранной группе бассейна — это не замена процедуры.
    return (
        prescription.procedure.is_generic_pool
        and booking.procedure_id == prescription.pool_group_id
    )


def _active_dates(program: Program, prescription: Prescription) -> list[date]:
    return [day for day in program.course_dates() if _is_active(prescription, program, day)]


def _is_active(prescription: Prescription, program: Program, day: date) -> bool:
    start = prescription.start_date or program.start_date
    return (
        program.is_therapy_day(day)
        and start <= day
        and (prescription.cancel_date is None or day < prescription.cancel_date)
    )


def _need(
    prescription: Prescription,
    dates: set[date],
    pinned: list[Booking],
    equipment: dict[int, Equipment],
    removed: Counter[tuple[int, date]] | None = None,
    previous: Counter[int] | None = None,
) -> Need:
    own = Counter(b.date for b in pinned if b.prescription_id == prescription.pk)
    # Убранное вручную занятие подбор считает закреплённым: ставит на столько же меньше.
    for (prescription_id, day), units in (removed or {}).items():
        if prescription_id == prescription.pk:
            own[day] += units
    procedure = prescription.procedure
    if procedure.kind == ProcedureKind.EQUIPMENT:
        item = equipment[procedure.equipment_id]
        return Need(
            prescription_id=prescription.pk,
            procedure_id=procedure.pk,
            kind=Kind.EQUIPMENT,
            label=procedure.card_label,
            dates=tuple(sorted(dates)),
            equipment_id=item.pk,
            starts=tuple(_minutes(t) for t in item.start_times()),
            duration=item.duration_min,
            capacity=item.capacity,
            per_day=prescription.per_day,
            # Закреплённое занятие — одно из «р/д» этого дня, остальные подбор добирает.
            pinned_units=tuple(sorted(own.items())),
            unavailable="" if item.is_active else f"тренажёр «{item.name}» выключен в справочнике",
        )
    group_required = procedure.is_generic_pool
    if group_required and prescription.pool_group is not None:
        # Занятия встают во время группы, которую выбрал специалист ФР.
        procedure, group_required = prescription.pool_group, False
    active = list(procedure.sessions.filter(is_active=True))
    sessions = tuple(Session(s.pk, _minutes(s.start_time), _minutes(s.end_time)) for s in active)
    # Выбранное время действует, пока занятие действует и принадлежит группе (решение 81).
    chosen = prescription.group_session_id
    if chosen not in {s.pk for s in active}:
        chosen = None
    return Need(
        prescription_id=prescription.pk,
        procedure_id=procedure.pk,
        kind=Kind(procedure.kind),
        label=procedure.name if procedure.kind == ProcedureKind.POOL else procedure.card_label,
        dates=tuple(sorted(dates)),
        sessions=sessions,
        per_day=prescription.per_day,
        # Закреплённое занятие — одно из «р/д» этого дня, остальные подбор добирает.
        pinned_units=tuple(sorted(own.items())),
        group_required=group_required,
        chosen_session_id=chosen,
        # Выбирать есть из чего: занятий в день больше, чем назначено (решение 81).
        choice_needed=chosen is None and len(active) > prescription.per_day,
        previous_session_ids=tuple(pk for pk, _count in (previous or Counter()).most_common()),
    )


def _equipment_load(program: Program, equipment_ids: list[int]) -> tuple[EquipmentLoad, ...]:
    others = Booking.objects.filter(
        ~Q(program=program),
        equipment_id__in=equipment_ids,
        date__range=(program.start_date, program.end_date),
    )
    return tuple(
        EquipmentLoad(b.equipment_id, b.date, _minutes(b.start), _minutes(b.end)) for b in others
    )


# --- Экран «Расписание групп» (TZ.md, FR-SCH-18) ------------------------------------------


@dataclass(frozen=True)
class Rescheduled:
    """Что изменилось после правки расписания групп или тренажёра (решение 81)."""

    programs: list[Program]  # расписание пересобрано
    reset: list[Prescription] = field(default_factory=list)  # выбор времени сброшен
    unpinned: list[Booking] = field(default_factory=list)  # закреплённые в старое время сняты
    displaced: list[Displaced] = field(default_factory=list)  # индивидуальные — в «Не распределены»
    # «Ведение группы» закончилось — слот инструктора свободен (решение 82)
    freed: list[staff_changes.FreedLead] = field(default_factory=list)
    # Для экрана (for_viewer): всего пересобрано и сколько программ других отделений скрыто.
    total: int | None = None
    hidden: int = 0


def for_viewer(result: Rescheduled | None, user: User) -> Rescheduled | None:
    """Итог для экрана: фамилии — только пациентов отделений пользователя (FR-ACC-2), чужие —
    числом. Группы и тренажёры общие, пересборка задевает и чужие отделения."""
    if result is None:
        return None
    own = set(departments_for(user).values_list("pk", flat=True))

    def mine(program: Program) -> bool:
        return program.department_id in own

    every = {p.pk: p for p in result.programs}
    for item in (*result.reset, *result.unpinned, *result.displaced):
        every.setdefault(item.program.pk, item.program)
    return Rescheduled(
        programs=[p for p in result.programs if mine(p)],
        reset=[p for p in result.reset if mine(p.program)],
        unpinned=[b for b in result.unpinned if mine(b.program)],
        displaced=[d for d in result.displaced if mine(d.program)],
        freed=result.freed,
        total=len(result.programs),
        hidden=sum(1 for p in every.values() if not mine(p)),
    )


def can_edit_group_schedule(user: User) -> bool:
    """Расписание групп и окна тренажёров — право «Групповые занятия». Группы и тренажёры —
    общие для центра, отделение не нужно."""
    return has_right(user, Right.GROUP_SCHEDULE)


def require_group_schedule(user: User, procedure: Procedure | None = None) -> None:
    if not can_edit_group_schedule(user):
        raise PermissionDenied("Нет права править групповые занятия.")
    # Группа отделения — только для тех, кто в этом отделении работает (решение 3).
    if procedure is not None and procedure.department_id is not None:
        require_right(user, Right.GROUP_SCHEDULE, procedure.department)


def group_schedule_procedures(user: User) -> QuerySet[Procedure]:
    """Группы, у которых специалист может править расписание."""
    groups = Procedure.objects.filter(kind__in=SESSION_KINDS, is_active=True, group_choice=False)
    if not can_edit_group_schedule(user):
        return groups.none()
    if not is_admin(user):
        groups = groups.filter(Q(department=None) | Q(department__in=departments_for(user)))
    return groups.distinct().order_by("kind", "name")


def group_sessions(user: User) -> list[GroupSession]:
    return list(
        GroupSession.objects.filter(procedure__in=group_schedule_procedures(user))
        .select_related("procedure")
        .order_by("start_time", "procedure__name")
    )


@transaction.atomic
def save_group_session(
    user: User, session: GroupSession, *, today: date | None = None
) -> Rescheduled:
    """Сохраняет занятие группы и сразу пересобирает расписание текущих программ с этой
    группой: группы следуют за расписанием групп так же, как за назначениями (решение 29).

    Выбранное время следует за занятием (это ссылка на него); выключили занятие — выбор
    сбрасывается. Закреплённые вручную в старое время снимаются, группа встаёт по новому
    расписанию; индивидуальные, на которые она легла, уходят в «Не распределены». Всё это
    возвращается списками для экрана (решение 81)."""
    require_group_schedule(user, session.procedure)
    old = GroupSession.objects.filter(pk=session.pk).values_list("procedure_id", flat=True)
    if session.pk and old and old[0] != session.procedure_id:
        # Занятие принадлежит группе: перенос в другую группу оставил бы у её пациентов
        # занятия «чужой» группы. Нужна другая группа — новое занятие.
        raise ScheduleError("Группу у занятия не меняют: добавьте занятие нужной группе.")
    session.full_clean()
    session._history_user = user
    today = today or board_days.today()
    reset: list[Prescription] = []
    unpinned: list[Booking] = []
    freed: list[staff_changes.FreedLead] = []

    def save() -> None:
        # Программы уже заблокированы — единый порядок блокировок.
        session.save()
        if not session.is_active:
            reset.extend(reset_choices([session.pk], user))
        unpinned.extend(stale_pinned([session.pk]))
        freed.extend(_end_group_leads(user, [session.procedure_id], [session.pk], today))

    with watch_displaced() as displaced:
        programs = replan_for_group(session.procedure_id, today=today, save=save)
    # Завершённые курсы не пересобираются — их закреплённые остаются и в список не идут.
    replanned = {program.pk for program in programs}
    unpinned = [booking for booking in unpinned if booking.program_id in replanned]
    return Rescheduled(programs, reset, unpinned, displaced, freed)


def _end_group_leads(
    user: User, procedure_ids: list[int], session_ids: list[int], today: date
) -> list[staff_changes.FreedLead]:
    """Занятие выключено или перенесено: «Ведение группы» на него заканчивается сегодня
    (решение 82). Вызывать под блокировкой программ, уже после сохранения занятия.

    Инструкторов блокируем в общем порядке: программы → тренажёры → инструкторы →
    шахматки. Пересборка следом снова возьмёт те же тренажёры — они уже наши, поэтому
    тренажёры программ с этой группой блокируем раньше инструкторов."""
    instructors = staff_changes.group_lead_instructors(session_ids)
    if not instructors:
        return []
    targets = Program.objects.filter(
        Q(prescriptions__procedure__in=procedure_ids)
        | Q(prescriptions__pool_group__in=procedure_ids),
        end_date__gte=today,
    ).values("pk")
    equipment = set(
        Prescription.objects.filter(
            program__in=targets, procedure__equipment__isnull=False
        ).values_list("procedure__equipment", flat=True)
    )
    list(Equipment.objects.select_for_update().filter(pk__in=equipment).order_by("pk"))
    list(Instructor.objects.select_for_update().filter(pk__in=instructors).order_by("pk"))
    return staff_changes.end_group_leads(user, session_ids, today)


def stale_pinned(session_ids: list[int]) -> list[Booking]:
    """Закреплённые вручную занятия, которые не совпадают с занятием группы: оно выключено
    или перенесено. Пересборка их снимет — список нужен для экрана (решение 81)."""
    result = []
    for booking in (
        Booking.objects.filter(pinned=True, group_session__in=session_ids)
        .select_related("program__department", "group_session")
        .order_by("date", "program__room")
    ):
        session = booking.group_session
        if not session.is_active or (booking.start, booking.end) != (
            session.start_time,
            session.end_time,
        ):
            result.append(booking)
    return result


@transaction.atomic
def save_equipment(
    user: User,
    equipment: Equipment,
    *,
    today: date | None = None,
    card_label: str | None = None,
    synonyms: list[str] | None = None,
) -> Rescheduled:
    """Окно, шаг, длительность и вместимость тренажёра; расписание программ с ним
    пересобирается сразу (ручные записи вне окна допустимы и остаются как есть).

    ``card_label`` / ``synonyms`` — подпись и синонимы его процедуры (решение 83); название
    процедуры следует за названием тренажёра, если они совпадали."""
    require_group_schedule(user)
    equipment.name = equipment.name.strip()
    if Equipment.objects.filter(name__iexact=equipment.name).exclude(pk=equipment.pk).exists():
        raise ScheduleError(f"Тренажёр «{equipment.name}» уже есть.")
    old_name = Equipment.objects.filter(pk=equipment.pk).values_list("name", flat=True).first()
    procedures = list(equipment.procedures.order_by("pk")) if equipment.pk else []
    for index, procedure in enumerate(procedures):
        if procedure.name == old_name:
            procedure.name = equipment.name
        if index == 0 and card_label is not None:
            procedure.card_label = card_label.strip() or equipment.name
        if index == 0 and synonyms is not None:
            procedure.synonyms = list(synonyms)
        if error := procedure_name_taken(procedure):
            raise ScheduleError(error)
        procedure.full_clean()
        procedure._history_user = user
    equipment.full_clean()
    equipment._history_user = user

    def save() -> None:
        equipment.save()
        for procedure in procedures:
            procedure.save()

    # Строка тренажёра меняется после блокировки программ: подбор блокирует программу раньше
    # тренажёра, обратный порядок дал бы взаимную блокировку.
    programs = replan_for_equipment(equipment.pk, today=today, save=save)
    return Rescheduled(programs)


# --- Разделы, группы и тренажёры на экране «Групповые занятия» (FR-SCH-18, решение 82) -----


def schedule_groups(user: User) -> QuerySet[Procedure]:
    """Группы, которые специалист видит на экране, — и выключенные (они внизу, серые)."""
    groups = Procedure.objects.filter(kind__in=SESSION_KINDS, group_choice=False)
    if not can_edit_group_schedule(user):
        return groups.none()
    if not is_admin(user):
        groups = groups.filter(Q(department=None) | Q(department__in=departments_for(user)))
    return groups.distinct()


def group_departments(user: User) -> QuerySet[Department]:
    """Отделения, которым специалист может завести группу: свои; администратор — любые."""
    return departments_for(user).order_by("name")


def sections_with_counts(user: User) -> list[Section]:
    """Разделы по порядку вкладок; ``sessions_count`` — действующих занятий действующих групп
    раздела, которые видит специалист (архив не считается)."""
    counts = dict(
        GroupSession.objects.filter(procedure__in=group_schedule_procedures(user), is_active=True)
        .values_list("procedure__section")
        .annotate(total=Count("pk"))
        .values_list("procedure__section", "total")
    )
    sections = list(Section.objects.order_by("order", "name"))
    for section in sections:
        section.sessions_count = counts.get(section.pk, 0)
    return sections


def section_groups(user: User, section: Section) -> list[Procedure]:
    """Группы раздела: ``session_list`` — действующие времена, ``archived_sessions`` — времена
    в архиве (выключенные). Действующие группы по названию, группы в архиве — в конце."""
    groups = list(
        schedule_groups(user)
        .filter(section=section)
        .select_related("department")
        .prefetch_related("sessions")
        .order_by("-is_active", "name")
    )
    for group in groups:
        every = sorted(group.sessions.all(), key=lambda s: (s.start_time, s.pk))
        group.session_list = [s for s in every if s.is_active]
        group.archived_sessions = [s for s in every if not s.is_active]
        group.active_sessions = len(group.session_list)
    return groups


@transaction.atomic
def save_section(user: User, section: Section) -> Section:
    """Новый раздел или правка: название, поведение, порядок. Поведение задаёт вид групп
    раздела, поэтому у раздела с группами его не меняют (решение 82)."""
    require_group_schedule(user)
    if section.pk:
        old = Section.objects.select_for_update().get(pk=section.pk)
        count = old.procedures.count()
        if old.behavior != section.behavior and count:
            raise ScheduleError(
                f"Поведение раздела «{old.name}» не меняется: в нём есть группы ({count}). "
                "Заведите новый раздел с нужным поведением."
            )
    section.full_clean()
    section._history_user = user
    section.save()
    return section


@transaction.atomic
def delete_section(user: User, section: Section) -> None:
    """Удалить можно только пустой раздел: группы удалять нельзя, только выключать."""
    require_group_schedule(user)
    section = Section.objects.select_for_update().get(pk=section.pk)
    count = section.procedures.count()
    if count:
        raise ScheduleError(
            f"В разделе «{section.name}» есть группы ({count}), в том числе выключенные — "
            "удалить можно только пустой раздел. Перенесите группы в другой раздел с тем же "
            "поведением."
        )
    section._history_user = user
    section.delete()


def procedure_name_taken(procedure: Procedure) -> str:
    """Текст ошибки, если в отделении (или среди общих) уже есть процедура с таким названием —
    повторяет ограничение ``unique_procedure_per_department`` понятным текстом."""
    same = Procedure.objects.filter(
        name__iexact=procedure.name.strip(), department=procedure.department_id
    )
    if procedure.pk:
        same = same.exclude(pk=procedure.pk)
    if not same.exists():
        return ""
    where = f"в отделении {procedure.department}" if procedure.department_id else "среди общих"
    return f"Процедура «{procedure.name.strip()}» уже есть в справочнике {where}."


def _check_group(group: Procedure, section: Section) -> None:
    group.name = group.name.strip()
    group.card_label = (group.card_label or "").strip() or group.name
    group.kind = section.behavior
    group.section = section
    if error := procedure_name_taken(group):
        raise ScheduleError(error)
    group.full_clean()


@dataclass(frozen=True)
class GroupSaved:
    """Итог правки группы на экране: что сохранено и что изменилось у программ."""

    group: Procedure
    created: bool = False
    rescheduled: Rescheduled | None = None  # группу выключили — расписание пересобрано


@transaction.atomic
def create_group(user: User, group: Procedure, times: list[time]) -> GroupSaved:
    """Новая группа и сразу её занятия (решение 82). Вид — из раздела; длительность занятий —
    длительность группы. Программ с новой группой ещё нет — пересобирать нечего."""
    # Группа отделения — только для тех, кто в нём работает (решение 3).
    require_group_schedule(user, group)
    if group.section_id is None:
        raise ScheduleError("Выберите раздел.")
    # Раздел блокируем: параллельная смена его поведения не должна разойтись с видом группы.
    section = Section.objects.select_for_update().get(pk=group.section_id)
    if not times:
        raise ScheduleError("Укажите хотя бы одно время занятия.")
    repeated = sorted({start for start in times if times.count(start) > 1})
    if repeated:
        raise ScheduleError(f"Время указано дважды: {', '.join(_hhmm(t) for t in repeated)}.")
    group.equipment = None
    group.group_choice = group.evening_individual = False
    group.is_active = True
    group.default_duration_min = group.default_duration_min or 30
    _check_group(group, section)
    group._history_user = user
    group.save()
    for start in sorted(times):
        session = GroupSession(
            procedure=group, start_time=start, duration_min=group.default_duration_min
        )
        session.full_clean()
        session._history_user = user
        session.save()
    return GroupSaved(group, created=True)


@transaction.atomic
def save_group(user: User, group: Procedure, *, today: date | None = None) -> GroupSaved:
    """Правка группы: названия, синонимы, место, отделение, раздел, «действует».

    Перенос — только в раздел с тем же поведением: вид группы определяет её место в
    расписании, а у текущих программ уже стоят её занятия. Выключение выключает и все её
    занятия — дальше как при выключении занятия (FR-CAT-3, решение 81): текущие программы
    пересобираются, выбор времени сбрасывается, закреплённые снимаются, и всё это — на экране.
    """
    # Строку группы не блокируем до программ: пересборка вставляет занятия со ссылкой на эту
    # процедуру (FOR KEY SHARE), и блокировка группы раньше программ замкнула бы цикл с
    # параллельной правкой назначения. Порядок как в save_group_session: программы → запись.
    old = Procedure.objects.filter(pk=group.pk).first()
    if old is None or not old.is_group:
        raise ScheduleError("Группа не найдена. Обновите страницу.")
    # И прежнее, и новое отделение группы — только свои (решение 3).
    require_group_schedule(user, old)
    require_group_schedule(user, group)
    if group.section_id is None:
        raise ScheduleError("Выберите раздел.")
    section = Section.objects.select_for_update().get(pk=group.section_id)
    if section.behavior != old.kind:
        behavior = SectionBehavior(old.kind).label
        raise ScheduleError(
            f"Группу «{old.name}» можно перенести только в раздел с поведением «{behavior}»: "
            "от поведения зависит её место в расписании пациентов и инструкторов. Нужна группа "
            "с другим поведением — заведите новую."
        )
    _check_group(group, section)
    group._history_user = user
    if not (old.is_active and not group.is_active):
        group.save()
        return GroupSaved(group)

    today = today or board_days.today()
    reset: list[Prescription] = []
    unpinned: list[Booking] = []
    freed: list[staff_changes.FreedLead] = []

    def save() -> None:
        # Программы уже заблокированы — единый порядок блокировок.
        group.save()
        sessions = list(group.sessions.filter(is_active=True))
        for session in sessions:
            session.is_active = False
            session._history_user = user
            session.save(update_fields=["is_active"])
        ids = [session.pk for session in sessions]
        reset.extend(reset_choices(ids, user))
        unpinned.extend(stale_pinned(ids))
        every = list(group.sessions.values_list("pk", flat=True))
        freed.extend(_end_group_leads(user, [group.pk], every, today))

    with watch_displaced() as displaced:
        programs = replan_for_group(group.pk, today=today, save=save)
    replanned = {program.pk for program in programs}
    unpinned = [booking for booking in unpinned if booking.program_id in replanned]
    return GroupSaved(group, rescheduled=Rescheduled(programs, reset, unpinned, displaced, freed))


# --- Удаление и архив (решение 82) -------------------------------------------------------
# Ни разу не использованное удаляется совсем; использованное уходит в архив — это выключение
# с пересборкой, сбросом выбора и окончанием «Ведения группы». Ссылки на занятие и группу:
# занятия пациентов (Booking), назначения (Prescription: procedure, pool_group,
# group_session), распорядок и разовые блоки инструкторов. Журнал (история) — не ссылка.


@dataclass(frozen=True)
class Removed:
    """Итог «Удалить»: удалено совсем или ушло в архив, и что изменилось у программ."""

    archived: bool
    rescheduled: Rescheduled | None = None


def session_is_used(session: GroupSession) -> bool:
    return (
        Booking.objects.filter(group_session=session).exists()
        or Prescription.objects.filter(group_session=session).exists()
        or InstructorDuty.objects.filter(group_session=session).exists()
        or InstructorBlock.objects.filter(group_session=session).exists()
    )


def group_is_used(group: Procedure) -> bool:
    return (
        Prescription.objects.filter(Q(procedure=group) | Q(pool_group=group)).exists()
        or Booking.objects.filter(procedure=group).exists()
        or any(session_is_used(session) for session in group.sessions.all())
    )


def affected_programs(group: Procedure, today: date | None = None) -> int:
    """Сколько текущих программ пересоберётся, если тронуть группу или её время."""
    return (
        Program.objects.filter(
            Q(prescriptions__procedure=group) | Q(prescriptions__pool_group=group),
            end_date__gte=today or board_days.today(),
        )
        .distinct()
        .count()
    )


@transaction.atomic
def delete_group_session(
    user: User, session: GroupSession, *, today: date | None = None
) -> Removed:
    """«Удалить» время группы: неиспользованное — совсем (программы с группой пересобираются:
    у них стало меньше вариантов времени), использованное — в архив."""
    require_group_schedule(user, session.procedure)
    if not session.is_active:
        raise ScheduleError("Это время уже в архиве.")
    if session_is_used(session):
        session.is_active = False
        return Removed(True, save_group_session(user, session, today=today))

    def delete() -> None:
        session._history_user = user
        try:
            session.delete()
        except ProtectedError:
            raise ScheduleError(
                "Это время только что начали использовать — обновите страницу."
            ) from None

    programs = replan_for_group(session.procedure_id, today=today, save=delete)
    return Removed(False, Rescheduled(programs))


@transaction.atomic
def restore_group_session(
    user: User, session: GroupSession, *, today: date | None = None
) -> Rescheduled:
    """«Вернуть из архива» время группы: оно снова действует, программы пересобираются."""
    if not session.procedure.is_active:
        raise ScheduleError("Сначала верните из архива саму группу.")
    session.is_active = True
    return save_group_session(user, session, today=today)


@transaction.atomic
def delete_group(user: User, group: Procedure, *, today: date | None = None) -> Removed:
    """«Удалить» группу: неиспользованную — совсем, вместе с её временами; использованную —
    в архив (выключение группы, ``save_group``)."""
    require_group_schedule(user, group)
    if not group.is_group:
        raise ScheduleError("Группа не найдена. Обновите страницу.")
    if not group.is_active:
        raise ScheduleError("Группа уже в архиве.")
    if group_is_used(group):
        # Архив — путь выключения: программы блокируются раньше строки группы (save_group).
        group.is_active = False
        return Removed(True, save_group(user, group, today=today).rescheduled)
    # «Совсем» программ не трогает — блокируем строку группы: новое назначение
    # (FOR KEY SHARE) подождёт и не сошлётся на удалённую. Под блокировкой проверяем снова.
    locked = Procedure.objects.select_for_update().get(pk=group.pk)
    # Её времена — тоже: занятие пациента ссылается и на время, и на группу.
    list(GroupSession.objects.select_for_update().filter(procedure=locked).order_by("pk"))
    if group_is_used(locked):
        raise ScheduleError("Группу только что начали использовать — обновите страницу.")
    for session in locked.sessions.all():
        session._history_user = user
        session.delete()
    locked._history_user = user
    locked.delete()
    return Removed(False)


@transaction.atomic
def restore_group(user: User, group: Procedure, *, today: date | None = None) -> GroupSaved:
    """«Вернуть из архива» группу: она снова в поиске и выборе; её времена остаются в архиве
    и возвращаются по одному."""
    group.is_active = True
    return save_group(user, group, today=today)


def equipment_is_used(equipment: Equipment) -> bool:
    """Занятия пациентов на тренажёре или назначения и занятия его процедур (решение 83)."""
    procedures = equipment.procedures.all()
    return (
        Booking.objects.filter(Q(equipment=equipment) | Q(procedure__in=procedures)).exists()
        or Prescription.objects.filter(procedure__in=procedures).exists()
    )


def equipment_programs(equipment: Equipment, today: date | None = None) -> int:
    """Сколько текущих программ с этим тренажёром пересоберётся."""
    return (
        Program.objects.filter(
            prescriptions__procedure__equipment=equipment,
            end_date__gte=today or board_days.today(),
        )
        .distinct()
        .count()
    )


@transaction.atomic
def delete_equipment(user: User, equipment: Equipment, *, today: date | None = None) -> Removed:
    """«Удалить» тренажёр на экране «Групповые занятия» (решение 83)."""
    require_group_schedule(user)
    return remove_equipment(user, equipment, today=today)


@transaction.atomic
def remove_equipment(user: User, equipment: Equipment, *, today: date | None = None) -> Removed:
    """Неиспользованный тренажёр — совсем, вместе с процедурой; использованный — в архив
    вместе с процедурой (её архив в «Справочниках» — это тоже архив тренажёра). Текущие
    программы пересобираются: занятий на нём больше нет, у назначения — конфликт «тренажёр
    выключен». Права проверяет вызывающий: «Групповые занятия» или «Справочники»."""
    if not equipment.is_active:
        raise ScheduleError("Тренажёр уже в архиве.")
    if not equipment_is_used(equipment):
        # Неиспользованный программ не трогает. Сначала процедуры, потом тренажёр: новое
        # назначение (FOR KEY SHARE на процедуру) ждёт здесь и не успеет дойти до пересборки,
        # которая блокирует тренажёр, — иначе мы ждали бы друг друга.
        list(Procedure.objects.select_for_update().filter(equipment=equipment).order_by("pk"))
        locked = Equipment.objects.select_for_update().get(pk=equipment.pk)
        if equipment_is_used(locked):
            raise ScheduleError("Тренажёр только что начали использовать — обновите страницу.")
        for procedure in locked.procedures.all():
            procedure._history_user = user
            procedure.delete()
        locked._history_user = user
        locked.delete()
        return Removed(False)
    return Removed(True, _set_equipment_active(user, equipment, False, today))


@transaction.atomic
def restore_equipment(
    user: User, equipment: Equipment, *, today: date | None = None
) -> Rescheduled:
    """«Вернуть из архива» тренажёр и его процедуру; программы с ним пересобираются."""
    require_group_schedule(user)
    return restore_equipment_now(user, equipment, today=today)


@transaction.atomic
def restore_equipment_now(
    user: User, equipment: Equipment, *, today: date | None = None
) -> Rescheduled:
    """Возврат тренажёра без проверки прав — для «Справочников» (права проверены там)."""
    return _set_equipment_active(user, equipment, True, today)


def _set_equipment_active(
    user: User, equipment: Equipment, active: bool, today: date | None
) -> Rescheduled:
    procedures = list(equipment.procedures.all())

    def save() -> None:
        equipment.is_active = active
        equipment._history_user = user
        equipment.save(update_fields=["is_active"])
        for procedure in procedures:
            procedure.is_active = active
            procedure._history_user = user
            procedure.save(update_fields=["is_active"])

    with watch_displaced() as displaced:
        programs = replan_for_equipment(equipment.pk, today=today, save=save)
    return Rescheduled(programs, displaced=displaced)


@transaction.atomic
def create_equipment(
    user: User, equipment: Equipment, *, card_label: str = "", synonyms: list[str] | None = None
) -> Equipment:
    """Новый тренажёр и его процедура вида «Тренажёр» — как в ``load_initial_catalog``:
    без процедуры тренажёр нельзя назначить (решение 82)."""
    require_group_schedule(user)
    equipment.name = equipment.name.strip()
    if Equipment.objects.filter(name__iexact=equipment.name).exists():
        raise ScheduleError(f"Тренажёр «{equipment.name}» уже есть.")
    procedure = Procedure(
        name=equipment.name,
        card_label=card_label.strip() or equipment.name,
        synonyms=list(synonyms or []),
        kind=ProcedureKind.EQUIPMENT,
        default_duration_min=equipment.duration_min,
        place=EQUIPMENT_PLACE,
    )
    if error := procedure_name_taken(procedure):
        raise ScheduleError(error)
    equipment.full_clean()
    equipment._history_user = user
    equipment.save()
    procedure.equipment = equipment
    procedure.full_clean()
    procedure._history_user = user
    procedure.save()
    return equipment


def replan_for_group(
    *procedure_ids: int, today: date | None = None, save: Callable[[], object] | None = None
) -> list[Program]:
    """Пересобирает текущие программы с этими группами — и как назначением, и как выбранной
    группой бассейна. Вызывается и из админки: расписание групп меняется не только экраном.
    ``save`` — сохранить изменённую запись справочника после блокировки программ."""
    return _replan_current(
        Q(prescriptions__procedure__in=procedure_ids)
        | Q(prescriptions__pool_group__in=procedure_ids),
        today or board_days.today(),
        save,
    )


def replan_for_equipment(
    equipment_id: int | None,
    *,
    today: date | None = None,
    save: Callable[[], object] | None = None,
) -> list[Program]:
    if equipment_id is None:
        # Новый тренажёр: программ с ним ещё нет.
        if save is not None:
            save()
        return []
    return _replan_current(
        Q(prescriptions__procedure__equipment=equipment_id), today or board_days.today(), save
    )


@transaction.atomic
def _replan_current(
    condition: Q, today: date, save: Callable[[], object] | None = None
) -> list[Program]:
    # Порядок блокировок везде один: программы (по pk) → тренажёры → инструкторы. Программы
    # блокируем все сразу и до сохранения справочника (``save``): иначе правка назначения
    # (программа → тренажёр) и эта пересборка (тренажёр → программа) ждали бы друг друга.
    targets = Program.objects.filter(condition, end_date__gte=today).values("pk")
    programs = list(
        Program.objects.select_for_update(of=("self",))
        .filter(pk__in=targets)
        .select_related("department")
        .order_by("pk")
    )
    if save is not None:
        save()
    for program in programs:
        replan(program)
    return programs


# --- Чтение: что показать на экране и в карте ----------------------------------------------


@dataclass(frozen=True)
class CalendarCell:
    bookings: list[Booking]
    deviation: bool
    active: bool
    # Сколько занятий в эту дату убрано вручную — можно вернуть.
    removed: int = 0
    # Индивидуальные — из шахматок (FR-SCH-16): в каком блоке пациент, если его нет в сетке,
    # и «шахматки ещё нет» для дат дальше завтрашней.
    individual: bool = False
    hold: str = ""
    pending: bool = False


@dataclass(frozen=True)
class CalendarRow:
    date: date
    cells: list[CalendarCell]  # по колонкам ProgramSchedule.columns


@dataclass(frozen=True)
class BoardLine:
    """Индивидуальные пациента в составленной шахматке на дату (FR-SCH-16)."""

    date: date
    text: str  # «9:10 Соколов, 14:20 Лебедева», «в блоке «Не распределены»», «занятий нет»
    hold: bool = False  # пациент в блоке — специалисту нужно поставить его в сетку


@dataclass(frozen=True)
class PoolGroupOption:
    procedure: Procedure
    times: str  # «9:00, 9:45, 14:15» — по времени специалист и выбирает группу


@dataclass(frozen=True)
class TimeOption:
    session: GroupSession
    text: str  # «11:15 (бассейн)»


@dataclass(frozen=True)
class GroupTimeChoice:
    """Строка блока «Время групп» (решение 81): назначение группы и из чего выбрать время."""

    prescription: Prescription
    label: str
    group: Procedure | None  # из чьих занятий выбирать; у «Бассейн» без группы — группа бассейна
    pool: bool  # «Бассейн» без группы — сначала выбирают группу
    options: list[TimeOption]
    auto: str  # где подбор поставил группу сам: «8:15»

    @property
    def chosen_id(self) -> int | None:
        session = self.prescription.group_session
        return session.pk if session is not None and self.prescription.session_fits() else None

    @property
    def needs_choice(self) -> bool:
        """Занятий в день больше назначенного, а время не выбрано — жёлтое предупреждение."""
        return (
            self.group is not None
            and self.chosen_id is None
            and len(self.options) > self.prescription.per_day
        )

    @property
    def group_missing(self) -> bool:
        return self.pool and self.group is None


@dataclass(frozen=True)
class ProgramSchedule:
    typical: list[TypicalRow]
    day: list[GridRow]  # типичный день по сетке слотов, с окнами — как в карте
    columns: list[Prescription]
    calendar: list[CalendarRow]
    issues: list[dict]
    # Время групп (решение 81) и группы бассейна, из которых выбирать (FR-PRG-3).
    group_times: list[GroupTimeChoice] = field(default_factory=list)
    pool_groups: list[PoolGroupOption] = field(default_factory=list)
    boards: list[BoardLine] = field(default_factory=list)

    @property
    def conflicts(self) -> list[dict]:
        return [issue for issue in self.issues if issue["conflict"]]

    @property
    def warnings(self) -> list[dict]:
        """Отклонения по дням. «Время выбрано автоматически» показывает блок «Время групп»."""
        return [
            issue
            for issue in self.issues
            if not issue["conflict"] and issue["code"] != IssueCode.GROUP_TIME_AUTO
        ]


def program_schedule(program: Program) -> ProgramSchedule:
    """Типичный день (FR-SCH-6) и календарь курса: строки — даты, колонки — назначения.

    Группы, бассейн и тренажёры — по всем дням курса. Индивидуальные живут в шахматке на день
    вперёд: в типичный день (и в карту) идут из ближайшей шахматки, где стоит пациент, —
    сегодняшней, иначе завтрашней (FR-SCH-16, FR-CRD-2)."""
    bookings = list(
        program.bookings.select_related(
            "procedure", "group_session__procedure", "prescription", "instructor__partner"
        )
    )
    today = board_days.today()
    individual = [b for b in bookings if b.kind == BookingKind.INDIVIDUAL]
    nearest = min((b.date for b in individual if b.date >= today), default=None)
    notes = _current_equipment_notes(bookings, today)
    items = [
        ScheduledItem(
            key=b.prescription_id,
            # Пометка тренажёра печатается в карте (FR-CRD-2): одна на назначение, иначе
            # типичный день взял бы подпись случайного дня.
            label=" ".join(filter(None, (b.procedure.card_label, notes.get(b.prescription_id)))),
            place=b.place,
            date=b.date,
            start=_minutes(b.start),
            end=_minutes(b.end),
            # Пара 2/2 — одна команда: «Волков/ Лебедева» в типичном дне не отклонение.
            who=b.instructor.team_label if b.instructor else "",
        )
        for b in bookings
        if b.kind != BookingKind.INDIVIDUAL or b.date == nearest
    ]
    typical = typical_day(items)
    # Колонки — все назначения, которые ставит подбор, даже если поставить не удалось:
    # пустая колонка тоже информация.
    columns = list(
        program.prescriptions.filter(procedure__kind__in=SCHEDULED_KINDS).select_related(
            "procedure"
        )
    )
    by_day: dict[tuple[date, int], list[Booking]] = defaultdict(list)
    for b in bookings:
        by_day[(b.date, b.prescription_id)].append(b)
    deviations = {(day, row.key) for row in typical for day in row.deviations}
    removed = {
        (item.date, item.prescription_id): item.units
        for item in RemovedSession.objects.filter(prescription__program=program)
    }
    boards = set(BoardDay.objects.filter(date__gte=today).values_list("date", flat=True))
    holds = {
        item.date: item for item in BoardPatient.objects.filter(program=program, date__in=boards)
    }
    calendar = [
        CalendarRow(
            day,
            [
                _calendar_cell(
                    program, column, day, by_day.get((day, column.pk), []),
                    deviation=(day, column.pk) in deviations,
                    removed=removed.get((day, column.pk), 0),
                    board=day in boards, past=day < today, hold=holds.get(day),
                )
                for column in columns
            ],
        )
        for day in program.course_dates()
    ]  # fmt: skip
    day = day_grid(typical, _day_slots())
    times = group_time_choices(program, bookings)
    options = [_pool_option(group) for group in pool_groups()] if any(t.pool for t in times) else []
    has_individual = any(c.procedure.kind == ProcedureKind.INDIVIDUAL for c in columns)
    lines = (
        [_board_line(day, individual, holds.get(day)) for day in sorted(boards)]
        if has_individual
        else []
    )
    return ProgramSchedule(
        typical,
        day,
        columns,
        calendar,
        list(program.schedule_issues),
        times,
        options,
        lines,
    )


def group_time_choices(
    program: Program, bookings: list[Booking] | None = None
) -> list[GroupTimeChoice]:
    """Блок «Время групп» (решение 81): назначения, у которых есть что выбирать — занятий
    в день больше назначенного, — время уже выбрано, или это «Бассейн» без группы."""
    if bookings is None:
        bookings = list(program.bookings.all())
    today = board_days.today()
    starts: dict[int, Counter[time]] = defaultdict(Counter)
    for booking in bookings:
        if booking.kind in GROUP_BOOKING_KINDS and not booking.pinned:
            starts[booking.prescription_id][booking.start] += 1
    result = []
    for item in program.prescriptions.select_related(
        "procedure", "pool_group", "group_session"
    ).filter(procedure__kind__in=SESSION_KINDS):
        if item.is_cancelled(today):
            continue
        group = item.choice_group
        sessions = (
            sorted(
                (s for s in group.sessions.all() if s.is_active), key=lambda s: (s.start_time, s.pk)
            )
            if group
            else []
        )
        pool = item.procedure.is_generic_pool
        if not (pool or item.group_session_id or len(sessions) > item.per_day):
            continue
        common = [start for start, _count in starts[item.pk].most_common(item.per_day)]
        result.append(
            GroupTimeChoice(
                prescription=item,
                label=item.label,
                group=group,
                pool=pool,
                options=[TimeOption(s, _session_text(s)) for s in sessions],
                auto=", ".join(_hhmm(t) for t in sorted(common)),
            )
        )
    return result


def _session_text(session: GroupSession) -> str:
    place = session.effective_place
    return _hhmm(session.start_time) + (f" ({place})" if place else "")


def _hhmm(value: time) -> str:
    return f"{value.hour}:{value.minute:02d}"


def _board_line(day: date, individual: list[Booking], hold: BoardPatient | None) -> BoardLine:
    seats = sorted((b for b in individual if b.date == day), key=lambda b: b.start)
    parts = [
        f"{b.start:%-H:%M} {b.instructor.short_name if b.instructor else ''}".strip()
        + (f" ({b.note})" if b.note else "")
        for b in seats
    ]
    if hold is not None and hold.unplaced:
        parts.append(f"в блоке «Не распределены» ({hold.unplaced})")
    if hold is not None and hold.cancelled:
        parts.append(f"в блоке «Отменены» ({hold.cancelled})")
    held = hold is not None and bool(hold.unplaced or hold.cancelled)
    return BoardLine(day, ", ".join(parts) or "занятий нет", held)


def _calendar_cell(
    program: Program,
    column: Prescription,
    day: date,
    bookings: list[Booking],
    *,
    deviation: bool,
    removed: int,
    board: bool,
    past: bool,
    hold: BoardPatient | None,
) -> CalendarCell:
    active = _is_active(column, program, day)
    if column.procedure.kind != ProcedureKind.INDIVIDUAL:
        return CalendarCell(bookings, deviation, active, removed)
    text = ""
    if hold is not None and hold.unplaced:
        text = "не распределён"
    elif hold is not None and hold.cancelled:
        text = "отменён"
    # Будни дальше составленных шахматок — индивидуальное ещё не ставилось; в выходные шахматки
    # нет вовсе.
    active = active and not is_weekend(day)
    pending = active and not bookings and not past and not board
    return CalendarCell(
        bookings, False, active, 0,
        individual=True, hold=text, pending=pending,
    )  # fmt: skip


def _pool_option(group: Procedure) -> PoolGroupOption:
    starts = sorted(s.start_time for s in group.sessions.all() if s.is_active)
    return PoolGroupOption(group, ", ".join(f"{t.hour}:{t.minute:02d}" for t in starts))


def _day_slots() -> list[tuple[int, int]]:
    """Дневная сетка слотов: её строки и есть строки блока «Расписание занятий» карты."""
    return [
        (_minutes(slot.start), _minutes(slot.end))
        for slot in InstructorSlot.objects.filter(is_evening=False)
    ]


def _minutes(value: time) -> int:
    return minutes_between(MIDNIGHT, value)


def _time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)
