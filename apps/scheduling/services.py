"""Расписание программы (TZ.md §7): подбор, хранение, типичный день."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet

from apps.accounts.access import departments_for, has_right, is_admin, require_right
from apps.accounts.models import Right, User
from apps.catalog.domain import minutes_between
from apps.catalog.models import (
    SCHEDULED_KINDS,
    SESSION_KINDS,
    Equipment,
    GroupSession,
    InstructorSlot,
    Procedure,
    ProcedureKind,
)
from apps.live import events as live
from apps.live import topics as live_topics
from apps.programs.models import Prescription, Program, withdrawn_on
from apps.staff.models import Instructor

from . import board_days
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

    def save() -> None:
        # Программы уже заблокированы — единый порядок блокировок.
        session.save()
        if not session.is_active:
            reset.extend(reset_choices([session.pk], user))
        unpinned.extend(stale_pinned([session.pk]))

    with watch_displaced() as displaced:
        programs = replan_for_group(session.procedure_id, today=today, save=save)
    # Завершённые курсы не пересобираются — их закреплённые остаются и в список не идут.
    replanned = {program.pk for program in programs}
    unpinned = [booking for booking in unpinned if booking.program_id in replanned]
    return Rescheduled(programs, reset, unpinned, displaced)


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
def save_equipment(user: User, equipment: Equipment, *, today: date | None = None) -> Rescheduled:
    """Окно, шаг, длительность и вместимость тренажёра; расписание программ с ним
    пересобирается сразу (ручные записи вне окна допустимы и остаются как есть)."""
    require_group_schedule(user)
    equipment.full_clean()
    equipment._history_user = user
    # Строка тренажёра меняется после блокировки программ: подбор блокирует программу раньше
    # тренажёра, обратный порядок дал бы взаимную блокировку.
    programs = replan_for_equipment(equipment.pk, today=today, save=equipment.save)
    return Rescheduled(programs)


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
    items = [
        ScheduledItem(
            key=b.prescription_id,
            label=b.procedure.card_label,
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
