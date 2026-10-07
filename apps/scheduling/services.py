"""Расписание программы (TZ.md §7): подбор, хранение, типичный день."""

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, time

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet

from apps.accounts.access import has_role, has_role_anywhere, is_admin, require_role
from apps.accounts.models import Role, User
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
from apps.programs.models import Prescription, Program

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
from .domain.model import Kind
from .models import BoardDay, BoardPatient, Booking, BookingKind, BookingSource, RemovedSession

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
    """Расписание правят специалист ФР и администратор (TZ.md §3)."""
    return has_role(user, program.department, Role.REHAB)


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
    require_role(user, program.department, Role.REHAB)
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
    prescription._history_user = user
    prescription.save(update_fields=["pool_group"])
    replan(program)


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
    for item in lifted:
        seats[item.date].append(board_days.Seat(item.instructor_id, item.slot_id, item.note))
    lifted._raw_delete(Booking.objects.db)

    bookings = list(program.bookings.all())
    # Автоматические занятия пересоздаются целиком и без журнала (TZ.md §13, п. 30):
    # иначе каждая правка назначения писала бы в историю сотни строк.
    automatic = [b.pk for b in bookings if not b.pinned and b.kind in AUTO_KINDS]
    Booking.objects.filter(pk__in=automatic)._raw_delete(Booking.objects.db)
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
    needs = tuple(_need(p, active[p.pk], pinned, equipment, removed) for p in prescriptions)
    snapshot = Snapshot(
        needs=needs,
        patient_busy=tuple(Busy(b.date, _minutes(b.start), _minutes(b.end)) for b in pinned),
        equipment_load=_equipment_load(program, equipment_ids),
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
    sessions = tuple(
        Session(s.pk, _minutes(s.start_time), _minutes(s.end_time))
        for s in procedure.sessions.filter(is_active=True)
    )
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
    """Что изменилось после правки расписания групп или тренажёра."""

    programs: list[Program]  # расписание пересобрано
    pinned: list[Booking]  # закреплены вручную в старое время — их специалист правит сам


def can_edit_group_schedule(user: User) -> bool:
    """Расписание групп и окна тренажёров правят специалист ФР и администратор. Группы и
    тренажёры — общие для центра, поэтому достаточно роли в любом отделении."""
    return has_role_anywhere(user, Role.REHAB)


def require_group_schedule(user: User, procedure: Procedure | None = None) -> None:
    if not can_edit_group_schedule(user):
        raise PermissionDenied("Расписание групп правит специалист ФР.")
    # Группа отделения — только для специалиста этого отделения (решение 3).
    if procedure is not None and procedure.department_id is not None:
        require_role(user, procedure.department, Role.REHAB)


def group_schedule_procedures(user: User) -> QuerySet[Procedure]:
    """Группы, у которых специалист может править расписание."""
    groups = Procedure.objects.filter(kind__in=SESSION_KINDS, is_active=True, group_choice=False)
    if not is_admin(user):
        groups = groups.filter(
            Q(department=None)
            | Q(department__memberships__user=user, department__memberships__role=Role.REHAB)
        )
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
    Закреплённые вручную занятия не трогаем — показываем их списком."""
    require_group_schedule(user, session.procedure)
    old = GroupSession.objects.filter(pk=session.pk).values_list("procedure_id", flat=True)
    if session.pk and old and old[0] != session.procedure_id:
        # Занятие принадлежит группе: перенос в другую группу оставил бы у её пациентов
        # занятия «чужой» группы. Нужна другая группа — новое занятие.
        raise ScheduleError("Группу у занятия не меняют: добавьте занятие нужной группе.")
    session.full_clean()
    session._history_user = user
    today = today or date.today()
    # Сохраняем после блокировки программ — единый порядок блокировок.
    programs = replan_for_group(session.procedure_id, today=today, save=session.save)
    pinned = list(
        Booking.objects.filter(pinned=True, group_session=session, date__gte=today)
        .exclude(start=session.start_time, end=session.end_time)
        .select_related("program__department")
        .order_by("date", "program__room")
    )
    return Rescheduled(programs, pinned)


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
    return Rescheduled(programs, [])


def replan_for_group(
    *procedure_ids: int, today: date | None = None, save: Callable[[], object] | None = None
) -> list[Program]:
    """Пересобирает текущие программы с этими группами — и как назначением, и как выбранной
    группой бассейна. Вызывается и из админки: расписание групп меняется не только экраном.
    ``save`` — сохранить изменённую запись справочника после блокировки программ."""
    return _replan_current(
        Q(prescriptions__procedure__in=procedure_ids)
        | Q(prescriptions__pool_group__in=procedure_ids),
        today or date.today(),
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
        Q(prescriptions__procedure__equipment=equipment_id), today or date.today(), save
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
    # Сколько занятий в эту дату убрано вручную (решение 57) — можно вернуть.
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
class ProgramSchedule:
    typical: list[TypicalRow]
    day: list[GridRow]  # типичный день по сетке слотов, с окнами — как в карте
    columns: list[Prescription]
    calendar: list[CalendarRow]
    issues: list[dict]
    # Назначения «Бассейн» без группы и группы, из которых выбирать (FR-PRG-3).
    pools: list[Prescription] = field(default_factory=list)
    pool_groups: list[PoolGroupOption] = field(default_factory=list)
    boards: list[BoardLine] = field(default_factory=list)

    @property
    def conflicts(self) -> list[dict]:
        return [issue for issue in self.issues if issue["conflict"]]

    @property
    def warnings(self) -> list[dict]:
        return [issue for issue in self.issues if not issue["conflict"]]


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
    pools = [
        p for p in program.prescriptions.select_related("procedure", "pool_group")
        if p.procedure and p.procedure.is_generic_pool
    ]  # fmt: skip
    options = [_pool_option(group) for group in pool_groups()] if pools else []
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
        pools,
        options,
        lines,
    )


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
