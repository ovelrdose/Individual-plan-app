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
from apps.programs.models import Prescription, Program
from apps.staff.models import Instructor
from apps.staff.services import active_instructors, build_teams, instructor_calendars

from .domain import (
    Assignment,
    Busy,
    EquipmentLoad,
    GridRow,
    Need,
    Proposal,
    ScheduledItem,
    Session,
    Slot,
    Snapshot,
    Staff,
    TypicalRow,
    day_grid,
    is_weekend,
    propose,
    typical_day,
)
from .domain.model import Kind
from .models import Booking, BookingKind, BookingSource, RemovedSession

# Что ставит подбор: группы ЛФК, бассейн, индивидуальные и тренажёры (TZ.md §7.3).
AUTO_KINDS = SCHEDULED_KINDS
MIDNIGHT = time(0, 0)


class ScheduleError(Exception):
    """Действие с расписанием невозможно. Текст показывается пользователю."""


def can_schedule(user: User, program: Program) -> bool:
    """Расписание правят специалист ФР и администратор (03-architecture.md §8)."""
    return has_role(user, program.department, Role.REHAB)


def replan_by(user: User, program: Program) -> Proposal:
    """«Подобрать заново»: всё, кроме закреплённых, — с нуля, без прошлых постановок
    индивидуальных (решение 50): специалист перераспределяет нагрузку."""
    require_role(user, program.department, Role.REHAB)
    return replan(program, keep_previous=False)


def preferred_instructor_options() -> list[Instructor]:
    """Из кого выбирать инструктора по желанию пациента: действующие, по порядку шахматки."""
    return active_instructors()


@transaction.atomic
def choose_preferred_instructor(
    user: User, program: Program, instructor: Instructor | None
) -> Proposal:
    """Инструктор по желанию пациента (TZ.md FR-PRG-1, решение 44). Пустое значение снимает
    выбор. Индивидуальные занятия сразу пересобираются: в его рабочие дни — у него."""
    program = (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=program.pk)
    )
    require_role(user, program.department, Role.REHAB)
    if (
        instructor is not None
        and not Instructor.objects.filter(pk=instructor.pk, is_active=True).exists()
    ):
        raise ScheduleError("Выберите действующего инструктора.")
    program.preferred_instructor = instructor
    program._history_user = user
    program.save(update_fields=["preferred_instructor", "updated_at"])
    return replan(program)


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
def replan(program: Program, *, keep_previous: bool = True) -> Proposal:
    """Перестраивает расписание программы: незакреплённые занятия групп, бассейна,
    индивидуальные и тренажёры удаляются и подбираются заново по текущим назначениям,
    датам курса, расписанию групп и сменам инструкторов.

    Вызывается после импорта и после любых изменений назначений и курса — специалисту не
    нужно помнить о «пересчитать». Закреплённые вручную занятия остаются, кроме тех, что
    оказались вне курса или вне дат назначения (FR-SCH-11, FR-SCH-12). Прошлые
    автоматические индивидуальные занятия сохраняются, если ещё допустимы (решение 49);
    ``keep_previous=False`` — подбор с нуля («Подобрать заново», решение 50).

    Порядок блокировок везде один: программа → тренажёры → инструкторы (по pk).
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

    bookings = list(program.bookings.all())
    previous = tuple(
        Assignment(b.prescription_id, b.date, b.instructor_id, b.slot_id)
        for b in bookings
        if keep_previous
        and not b.pinned
        and b.kind == BookingKind.INDIVIDUAL
        and b.instructor_id is not None
    )
    # Автоматические занятия пересоздаются целиком и без журнала (TZ.md §13, п. 30):
    # иначе каждая правка назначения писала бы в историю сотни строк.
    automatic = [b.pk for b in bookings if not b.pinned and b.kind in AUTO_KINDS]
    Booking.objects.filter(pk__in=automatic)._raw_delete(Booking.objects.db)
    # Закреплённые вручную — любого вида — остаются, пока подходят к назначению.
    # Выпавшие из курса или из дат назначения удаляются обычным путём, с записью в журнал.
    for booking in bookings:
        if booking.pinned and not _still_valid(booking, every, active):
            booking.delete()
    pinned = list(program.bookings.all())

    equipment_ids = sorted(
        {p.procedure.equipment_id for p in prescriptions if p.procedure.equipment_id}
    )
    # Блокируем тренажёры по порядку: два параллельных подбора не превысят вместимость
    # и не заблокируют друг друга.
    equipment = {
        e.pk: e
        for e in Equipment.objects.select_for_update().filter(pk__in=equipment_ids).order_by("pk")
    }

    removed: Counter[tuple[int, date]] = Counter()
    for item in RemovedSession.objects.filter(prescription__program=program):
        removed[(item.prescription_id, item.date)] += item.units
    needs = tuple(_need(p, active[p.pk], pinned, equipment, removed) for p in prescriptions)
    individual_dates = {day for need in needs if need.kind == Kind.INDIVIDUAL for day in need.dates}
    snapshot = Snapshot(
        needs=needs,
        patient_busy=tuple(
            Busy(
                b.date,
                _minutes(b.start),
                _minutes(b.end),
                individual=b.kind == BookingKind.INDIVIDUAL,
            )
            for b in pinned
        ),
        equipment_load=_equipment_load(program, equipment_ids),
        staff=_staff(program, previous) if individual_dates else Staff(),
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
                instructor_id=placement.instructor_id,
                slot_id=placement.slot_id,
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
    return start <= day and (prescription.cancel_date is None or day < prescription.cancel_date)


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
    if procedure.kind == ProcedureKind.INDIVIDUAL:
        return Need(
            prescription_id=prescription.pk,
            procedure_id=procedure.pk,
            kind=Kind.INDIVIDUAL,
            label=procedure.name,
            dates=tuple(sorted(dates)),
            per_day=prescription.per_day,
            pinned_units=tuple(sorted(own.items())),
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
        label=procedure.card_label if procedure.kind == ProcedureKind.LFK_GROUP else procedure.name,
        dates=tuple(sorted(dates)),
        sessions=sessions,
        per_day=prescription.per_day,
        # Закреплённое занятие — одно из «р/д» этого дня, остальные подбор добирает.
        pinned_units=tuple(sorted(own.items())),
        group_required=group_required,
    )


def _staff(program: Program, previous: tuple[Assignment, ...]) -> Staff:
    """Инструкторы для шага 3: кто свободен в дату и слот, загрузка за даты курса.

    Строки инструкторов блокируются (по pk, после программы и тренажёров): два параллельных
    подбора разных программ не займут один слот инструктора — второй ждёт и видит
    занятия первого (FR-SCH-3). Запросы — на всех инструкторов сразу, без N+1.
    """
    instructors = list(
        Instructor.objects.select_for_update(of=("self",))
        .filter(is_active=True)
        .select_related("partner")
        .order_by("pk")
    )
    start, end = program.start_date, program.end_date
    slots = list(InstructorSlot.objects.all())
    calendars = instructor_calendars(instructors, start, end)
    taken: set[tuple[date, int, int]] = set()
    load: Counter[int] = Counter()
    others = Booking.objects.filter(
        ~Q(program=program),
        kind=BookingKind.INDIVIDUAL,
        instructor__in=instructors,
        date__range=(start, end),
    ).values_list("date", "slot_id", "instructor_id")
    for item in others:
        taken.add(item)
        if not is_weekend(item[0]):
            load[item[2]] += 1
    day_slots = [slot.pk for slot in slots if not slot.is_evening]
    # Инструктор подбирается только на будни (решение 56).
    weekdays = [day for day in program.course_dates() if not is_weekend(day)]
    free = frozenset(
        (day, slot_id, item.pk)
        for item in instructors
        for day in weekdays
        for slot_id in calendars[item.pk].day(day).free_slots(day_slots)
        if (day, slot_id, item.pk) not in taken
    )
    # По графику, без исключений: больничный — не штатный выходной, замена в этот день —
    # отклонение, которое специалист должен видеть (FR-SCH-13).
    working = frozenset(
        (day, item.pk)
        for item in instructors
        for day in weekdays
        if calendars[item.pk].by_pattern(day)
    )
    ordered = sorted(instructors, key=lambda i: (i.display_order, i.short_name))
    return Staff(
        slots=tuple(
            Slot(slot.pk, _minutes(slot.start), _minutes(slot.end), slot.is_evening)
            for slot in slots
        ),
        teams=tuple(build_teams(ordered)),
        free=free,
        working=working,
        load=tuple(sorted(load.items())),
        preferred=program.preferred_instructor_id,
        preferred_name=(
            program.preferred_instructor.short_name if program.preferred_instructor else ""
        ),
        previous=previous,
        max_per_day=program.department.max_individual_per_day,
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
    # Сохраняем после блокировки программ — единый порядок блокировок (03-architecture.md §4.5).
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
    # тренажёра, обратный порядок дал бы взаимную блокировку (03-architecture.md §4.5).
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


@dataclass(frozen=True)
class CalendarRow:
    date: date
    cells: list[CalendarCell]  # по колонкам ProgramSchedule.columns


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
    # Есть индивидуальные — показываем выбор инструктора по желанию пациента (решение 44).
    individual: bool = False
    preferred: Instructor | None = None
    instructors: list[Instructor] = field(default_factory=list)

    @property
    def conflicts(self) -> list[dict]:
        return [issue for issue in self.issues if issue["conflict"]]

    @property
    def warnings(self) -> list[dict]:
        return [issue for issue in self.issues if not issue["conflict"]]


def program_schedule(program: Program) -> ProgramSchedule:
    """Типичный день (FR-SCH-6) и календарь курса: строки — даты, колонки — назначения."""
    bookings = list(
        program.bookings.select_related(
            "procedure", "group_session__procedure", "prescription", "instructor__partner"
        )
    )
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
    ]
    typical = typical_day(items)
    # Колонки — все назначения, которые ставит подбор, даже если поставить не удалось:
    # пустая колонка тоже информация.
    columns = list(
        program.prescriptions.filter(procedure__kind__in=AUTO_KINDS).select_related("procedure")
    )
    by_day: dict[tuple[date, int], list[Booking]] = defaultdict(list)
    for b in bookings:
        by_day[(b.date, b.prescription_id)].append(b)
    deviations = {(day, row.key) for row in typical for day in row.deviations}
    removed = {
        (item.date, item.prescription_id): item.units
        for item in RemovedSession.objects.filter(prescription__program=program)
    }
    calendar = [
        CalendarRow(
            day,
            [
                CalendarCell(
                    by_day.get((day, column.pk), []),
                    (day, column.pk) in deviations,
                    _is_active(column, program, day),
                    removed.get((day, column.pk), 0),
                )
                for column in columns
            ],
        )
        for day in program.course_dates()
    ]
    day = day_grid(typical, _day_slots())
    pools = [
        p for p in program.prescriptions.select_related("procedure", "pool_group")
        if p.procedure and p.procedure.is_generic_pool
    ]  # fmt: skip
    options = [_pool_option(group) for group in pool_groups()] if pools else []
    individual = any(c.procedure.kind == ProcedureKind.INDIVIDUAL for c in columns)
    preferred = program.preferred_instructor
    return ProgramSchedule(
        typical,
        day,
        columns,
        calendar,
        list(program.schedule_issues),
        pools,
        options,
        individual=individual or preferred is not None,
        preferred=preferred,
        instructors=preferred_instructor_options() if individual or preferred else [],
    )


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
