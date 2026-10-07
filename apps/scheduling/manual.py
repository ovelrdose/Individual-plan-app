"""Ручная правка расписания программы (TZ.md FR-SCH-9/10) — общая для шахматки и страницы
программы.

Любая правка проходит единый валидатор (FR-SCH-5) и закрепляется (``pinned``); после неё
расписание программы пересобирается, чтобы предупреждения и конфликты были свежими. Убранное
занятие запоминается (``RemovedSession``, решение 57) — подбор его не возвращает.

Порядок блокировок тот же, что у подбора: программа → тренажёры → инструкторы (по pk).
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, time

from django.db import IntegrityError, transaction

from apps.accounts.access import has_role
from apps.accounts.models import Role, User
from apps.catalog.domain import add_minutes
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, ProcedureKind
from apps.programs.models import Prescription, Program
from apps.staff.models import Instructor
from apps.staff.services import instructor_calendars

from .domain.validate import (
    Candidate,
    Context,
    Severity,
    Taken,
    Violation,
    ViolationCode,
    validate,
)
from .models import GROUP_BOOKING_KINDS, Booking, BookingKind, BookingSource, RemovedSession
from .services import replan


class EditError(Exception):
    """Правку сохранить нельзя: текст для пользователя."""

    def __init__(self, message: str, violations: list[Violation] | None = None):
        super().__init__(message)
        self.violations = violations or []


class ConfirmationRequired(EditError):
    """Нарушения «с подтверждением» или «с пометкой» (§7.2): нужна причина."""


ONE, FOLLOWING = "one", "following"


@dataclass
class EditResult:
    """Что изменено: даты, и какие даты пропущены и почему (для «эта и все следующие»)."""

    changed: list[date] = field(default_factory=list)
    skipped: list[tuple[date, str]] = field(default_factory=list)


# --- Права и блокировки ---------------------------------------------------------------------


def can_edit(user: User, program: Program) -> bool:
    """Расписание программы правит специалист ФР её отделения и администратор (решение 37)."""
    return has_role(user, program.department, Role.REHAB)


def require_edit(user: User, program: Program) -> None:
    if not can_edit(user, program):
        raise EditError("Расписание правит специалист ФР отделения пациента.")


def lock_program(program: Program) -> Program:
    return (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=program.pk)
    )


def lock_resources(program: Program) -> None:
    """Тренажёры программы и инструкторы — в том же порядке, что у подбора (программа уже
    заблокирована): иначе правка и параллельная пересборка ждали бы друг друга."""
    equipment = sorted(
        {
            p.procedure.equipment_id
            for p in program.prescriptions.select_related("procedure")
            if p.procedure is not None and p.procedure.equipment_id is not None
        }
    )
    list(Equipment.objects.select_for_update().filter(pk__in=equipment).order_by("pk"))
    list(Instructor.objects.select_for_update(of=("self",)).filter(is_active=True).order_by("pk"))


def fresh(program: Program, booking: Booking, version: int) -> Booking:
    """Запись ещё та, что видел пользователь (FR-SCH-4)."""
    current = (
        program.bookings.select_related("prescription__procedure", "procedure", "equipment")
        .filter(pk=booking.pk)
        .first()
    )
    if current is None or current.version != version:
        raise EditError("Запись изменена другим пользователем — обновите страницу.")
    return current


# --- Убранные занятия -----------------------------------------------------------------------


def add_removal(user: User, prescription: Prescription, day: date) -> None:
    removal, created = RemovedSession.objects.get_or_create(prescription=prescription, date=day)
    if not created:
        removal.units += 1
    removal._history_user = user
    removal.save()


def undo_removal(user: User, prescription: Prescription, day: date) -> bool:
    """Занятие снова ставят в дату, где его убирали: пометка «убрано» уменьшается."""
    removal = RemovedSession.objects.filter(prescription=prescription, date=day).first()
    if removal is None:
        return False
    removal._history_user = user
    if removal.units > 1:
        removal.units -= 1
        removal.save()
    else:
        removal.delete()
    return True


# --- Проверка -------------------------------------------------------------------------------


def minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def is_active_on(prescription: Prescription, program: Program, day: date) -> bool:
    start = prescription.start_date or program.start_date
    return (
        program.is_therapy_day(day)
        and start <= day
        and (prescription.cancel_date is None or day < prescription.cancel_date)
    )


def check(
    program: Program,
    prescription: Prescription,
    day: date,
    start: time,
    end: time,
    *,
    moved: Booking | None = None,
    instructor: Instructor | None = None,
    slot: InstructorSlot | None = None,
    equipment: Equipment | None = None,
) -> list[Violation]:
    """Нарушения кандидата: пациент, инструктор (для индивидуального), тренажёр."""
    if program.is_absent(day):
        raise EditError(f"Пациент выбыл — {day:%d.%m} у него нет занятий.")
    others = program.bookings.filter(date=day).select_related("procedure")
    if moved is not None and moved.pk:
        others = others.exclude(pk=moved.pk)
    individual = prescription.procedure.kind == ProcedureKind.INDIVIDUAL
    context = Context(
        patient=tuple(
            Taken(
                minutes(b.start),
                minutes(b.end),
                b.procedure.card_label or b.procedure.name,
                individual=b.kind == BookingKind.INDIVIDUAL,
            )
            for b in others
        ),
        in_course=is_active_on(prescription, program, day),
        individual_limit=_individual_limit(program),
    )
    if individual and instructor is not None and slot is not None:
        calendar = instructor_calendars([instructor], day, day)[instructor.pk].day(day)
        busy = calendar.busy.get(slot.pk)
        # Пациентов в ячейке может быть несколько (FR-SCH-10): занятость — только обязанности.
        context = _replace(
            context,
            instructor_working=calendar.working,
            instructor_busy=busy.text if busy else "",
        )
    if equipment is not None:
        taken = (
            Booking.objects.filter(
                date=day, kind=BookingKind.EQUIPMENT, equipment=equipment, start=start
            )
            .exclude(pk=moved.pk if moved is not None and moved.pk else None)
            .count()
        )
        context = _replace(
            context,
            equipment_starts=tuple(minutes(t) for t in equipment.start_times()),
            equipment_taken=taken,
            equipment_capacity=equipment.capacity,
        )
    candidate = Candidate(
        day,
        minutes(start),
        minutes(end),
        individual=individual,
        equipment=equipment is not None,
    )
    return validate(candidate, context)


def _replace(context: Context, **fields) -> Context:
    return replace(context, **fields)


def _individual_limit(program: Program) -> int:
    per_day = sum(
        item.per_day
        for item in program.prescriptions.select_related("procedure")
        if item.procedure is not None and item.procedure.kind == ProcedureKind.INDIVIDUAL
    )
    return min(per_day, program.department.max_individual_per_day)


def split(violations: list[Violation]) -> tuple[list[Violation], list[Violation]]:
    hard = [v for v in violations if v.severity is Severity.BLOCKING]
    soft = [v for v in violations if v.severity is not Severity.BLOCKING]
    return hard, soft


def save_manual(user: User, booking: Booking, reason: str = "") -> Booking:
    """Записать ручную правку: закрепить, поднять версию, автор и причина — в журнал."""
    booking.pinned = True
    booking.source = BookingSource.MANUAL
    if booking.pk:
        booking.version += 1
    booking._history_user = user
    if reason.strip():
        booking._change_reason = reason.strip()[:100]
    try:
        with transaction.atomic():
            booking.save()
    except IntegrityError:
        raise EditError("Это время только что заняли — обновите страницу.") from None
    return booking


# --- Правка занятия со страницы программы (FR-SCH-9) ----------------------------------------


@dataclass(frozen=True)
class Target:
    """Куда переставить занятие: для группы — занятие группы, для тренажёра — время начала."""

    session: GroupSession | None = None
    start: time | None = None


@transaction.atomic
def edit_booking(
    user: User,
    booking: Booking,
    version: int,
    target: Target,
    *,
    scope: str = ONE,
    reason: str = "",
) -> EditResult:
    """Изменить занятие на эту дату или «эту и все следующие» (FR-SCH-9).

    Эта дата должна пройти проверку — иначе ничего не сохраняется. Следующие даты, где
    вариант невозможен (инструктор не работает, время занято), пропускаются и
    перечисляются. Нарушения «с подтверждением» сохраняются только с причиной."""
    _require_not_individual(booking)
    program = lock_program(booking.program)
    require_edit(user, program)
    booking = fresh(program, booking, version)
    lock_resources(program)
    items = [booking] + (_following(program, booking) if scope == FOLLOWING else [])
    plans: list[tuple[Booking, Callable[[], None], bool]] = []
    result = EditResult()
    soft_all: list[Violation] = []
    for index, item in enumerate(items):
        try:
            apply, violations = _plan(program, item, target)
        except EditError as error:
            if index == 0:
                raise
            result.skipped.append((item.date, str(error)))
            continue
        hard, soft = split(violations)
        if hard:
            if index == 0:
                raise EditError(" ".join(v.message for v in hard), hard)
            result.skipped.append((item.date, " ".join(v.message for v in hard)))
            continue
        soft_all += [Violation(v.code, f"{item.date:%d.%m}: {v.message}") for v in soft]
        over = any(v.code == ViolationCode.EQUIPMENT_FULL for v in soft)
        plans.append((item, apply, over))
    if soft_all and not reason.strip():
        raise ConfirmationRequired(" ".join(v.message for v in soft_all), soft_all)
    for item, apply, over in plans:
        apply()
        if over:
            # Сверх вместимости — только с пометкой (FR-CAT-4a): она видна в шахматке.
            item.note = reason.strip()[:100]
        save_manual(user, item, reason)
        result.changed.append(item.date)
    replan(program)
    return result


def _following(program: Program, booking: Booking) -> list[Booking]:
    """То же занятие в следующие даты: того же назначения, ближайшее по времени к этому."""
    later = program.bookings.filter(
        prescription=booking.prescription, date__gt=booking.date
    ).order_by("date", "start")
    by_day: dict[date, list[Booking]] = {}
    for item in later:
        by_day.setdefault(item.date, []).append(item)
    base = minutes(booking.start)
    return [
        min(items, key=lambda b: (abs(minutes(b.start) - base), b.start))
        for _day, items in sorted(by_day.items())
    ]


def _plan(
    program: Program, booking: Booking, target: Target
) -> tuple[Callable[[], None], list[Violation]]:
    """Новое положение занятия в его дату и нарушения. Изменения — в apply()."""
    kind = booking.kind
    day = booking.date
    if kind in GROUP_BOOKING_KINDS:
        session = target.session
        if session is None or session.procedure_id != booking.procedure_id:
            raise EditError("Выберите время группы.")
        violations = check(
            program,
            booking.prescription,
            day,
            session.start_time,
            session.end_time,
            moved=booking,
        )

        def apply() -> None:
            booking.group_session = session
            booking.start, booking.end = session.start_time, session.end_time

        return apply, violations
    if kind == BookingKind.EQUIPMENT:
        if target.start is None:
            raise EditError("Выберите время тренажёра.")
        equipment = booking.equipment
        end = add_minutes(target.start, equipment.duration_min)
        violations = check(
            program,
            booking.prescription,
            day,
            target.start,
            end,
            moved=booking,
            equipment=equipment,
        )

        def apply() -> None:
            booking.start, booking.end = target.start, end

        return apply, violations
    raise EditError("Это занятие вручную не правится.")


def _require_not_individual(booking: Booking) -> None:
    if booking.kind == BookingKind.INDIVIDUAL:
        raise EditError("Индивидуальные занятия правятся в шахматке.")


@transaction.atomic
def remove_booking(user: User, booking: Booking, version: int, *, scope: str = ONE) -> EditResult:
    """Убрать занятие на дату или «эту и все следующие»: подбор его не вернёт (решение 57)."""
    _require_not_individual(booking)
    program = lock_program(booking.program)
    require_edit(user, program)
    booking = fresh(program, booking, version)
    items = [booking] + (_following(program, booking) if scope == FOLLOWING else [])
    result = EditResult()
    for item in items:
        add_removal(user, item.prescription, item.date)
        item._history_user = user
        item.delete()
        result.changed.append(item.date)
    replan(program)
    return result


@transaction.atomic
def restore_date(user: User, prescription: Prescription, day: date) -> None:
    """«Вернуть занятие»: снять пометку «убрано» на дату — подбор поставит его снова."""
    program = lock_program(prescription.program)
    require_edit(user, program)
    if not undo_removal(user, prescription, day):
        raise EditError("На эту дату занятие не убирали.")
    replan(program)


@transaction.atomic
def unpin(user: User, booking: Booking, version: int, *, scope: str = ONE) -> EditResult:
    """«Открепить» (FR-SCH-10): подбор снова распоряжается занятием (этой даты или всех
    закреплённых занятий назначения начиная с неё)."""
    _require_not_individual(booking)
    program = lock_program(booking.program)
    require_edit(user, program)
    booking = fresh(program, booking, version)
    if scope == FOLLOWING:
        items = list(
            program.bookings.filter(
                prescription=booking.prescription, date__gte=booking.date, pinned=True
            )
        )
    else:
        items = [booking] if booking.pinned else []
    result = EditResult()
    for item in items:
        item.pinned = False
        item.version += 1
        item._history_user = user
        item.save()
        result.changed.append(item.date)
    replan(program)
    return result
