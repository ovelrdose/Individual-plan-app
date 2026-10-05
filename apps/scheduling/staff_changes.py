"""Перестройка после изменения смены или распорядка инструктора (TZ.md FR-SCH-13,
FR-STF-6, решение 43).

Инструктор заболел, получил блок или новую обязанность — его индивидуальные занятия, которые
стали невозможны, система переставляет сама и только на затронутые даты: в тот же слот к
другому свободному инструктору (напарник, инструктор по желанию пациента, наименее
загруженный), иначе на ближайшее время. Переносятся и закреплённые вручную занятия — после
замены они снова закреплены. Специалист видит сводку и может «Вернуть как было».

Перестройка идёт отдельной транзакцией после изменения смены: порядок блокировок тот же, что
у подбора (программа → тренажёры → инструкторы).
"""

from collections import defaultdict
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import InstructorSlot
from apps.programs.models import Program
from apps.staff import services as staff_services
from apps.staff.models import Instructor, InstructorBlock, InstructorDuty, ShiftPattern
from apps.staff.services import instructor_calendars

from . import manual
from .domain.validate import ViolationCode
from .models import Booking, BookingKind, StaffChange
from .services import replan


def rebuild(
    user: User, instructor: Instructor, start: date, end: date | None, title: str
) -> StaffChange | None:
    """Переставить занятия инструктора, ставшие невозможными в ``start…end`` (``None`` —
    до последнего его занятия). Ничего не затронуто — None, сводки нет."""
    bookings = Booking.objects.filter(
        kind=BookingKind.INDIVIDUAL, instructor=instructor, date__gte=start
    )
    if end is not None:
        bookings = bookings.filter(date__lte=end)
    bookings = list(bookings.select_related("program", "slot"))
    if not bookings:
        return None
    first = min(b.date for b in bookings)
    last = max(b.date for b in bookings)
    calendar = instructor_calendars([instructor], first, last)[instructor.pk]
    affected = [b for b in bookings if not calendar.day(b.date).is_free(b.slot_id)]
    if not affected:
        return None
    by_program: dict[int, list[int]] = defaultdict(list)
    for item in affected:
        by_program[item.program_id].append(item.pk)
    items: list[dict] = []
    for program_id in sorted(by_program):
        items += _rebuild_program(user, program_id, by_program[program_id])
    change = StaffChange.objects.create(
        created_by=user, instructor=instructor, title=title, items=items
    )
    return change


@transaction.atomic
def _rebuild_program(user: User, program_id: int, booking_ids: list[int]) -> list[dict]:
    program = manual.lock_program(Program.objects.get(pk=program_id))
    affected = list(
        program.bookings.filter(pk__in=booking_ids).select_related("instructor", "slot")
    )
    # Места, которые у назначения в эту дату остаются (второе занятие в день и т. п.).
    before: dict[tuple[int, date], set[tuple[int | None, int | None]]] = {}
    for item in affected:
        key = (item.prescription_id, item.date)
        seats = set(
            program.bookings.filter(prescription_id=item.prescription_id, date=item.date)
            .exclude(pk__in=booking_ids)
            .values_list("instructor_id", "slot_id")
        )
        before[key] = seats
    preferred = program.preferred_instructor_id
    records = [
        {
            "program_id": program.pk,
            "department": program.department.name,
            "label": f"{program.room}п {program.surname}",
            "prescription_id": item.prescription_id,
            "date": item.date.isoformat(),
            "before": _seat(item),
            "after": None,
            "pinned": item.pinned,
            "preferred": preferred is not None and item.instructor_id == preferred,
        }
        for item in affected
    ]
    # Закреплённое тоже переносится (решение 43): подбор ставит замену, закрепление
    # возвращается на новое место.
    for item in affected:
        if item.pinned:
            item.pinned = False
            item.version += 1
            item._history_user = user
            item._change_reason = "перестройка: инструктор недоступен"
            item.save()
    replan(program)
    for record in records:
        key = (record["prescription_id"], date.fromisoformat(record["date"]))
        new = (
            program.bookings.filter(prescription_id=key[0], date=key[1], kind="INDIVIDUAL")
            .select_related("instructor", "slot")
            .order_by("start")
        )
        fresh = [b for b in new if (b.instructor_id, b.slot_id) not in before[key]]
        if not fresh:
            continue
        booking = fresh[0]
        before[key].add((booking.instructor_id, booking.slot_id))
        record["after"] = _seat(booking)
        if record["pinned"]:
            booking.pinned = True
            booking._history_user = user
            booking._change_reason = "перестройка: замена закреплённого"
            booking.save()
    return records


def _seat(booking: Booking) -> dict:
    return {
        "instructor_id": booking.instructor_id,
        "instructor": booking.instructor.short_name if booking.instructor else "",
        "slot_id": booking.slot_id,
        "start": f"{booking.start:%H:%M}",
    }


# --- «Вернуть как было» ---------------------------------------------------------------------


def revert(user: User, change: StaffChange) -> list[str]:
    """Вернуть занятия на прежние места, где это снова возможно (инструктор вышел, блок снят).
    Возвращает список того, что вернуть не удалось."""
    if not staff_services.can_manage_staff(user):
        raise manual.EditError("Вернуть может специалист ФР.")
    if change.reverted_at is not None:
        raise manual.EditError("Уже возвращено.")
    problems: list[str] = []
    by_program: dict[int, list[dict]] = defaultdict(list)
    for item in change.items:
        by_program[item["program_id"]].append(item)
    for program_id in sorted(by_program):
        problems += _revert_program(user, program_id, by_program[program_id])
    change.reverted_at = timezone.now()
    change.reverted_by = user
    change.save(update_fields=["reverted_at", "reverted_by"])
    return problems


@transaction.atomic
def _revert_program(user: User, program_id: int, items: list[dict]) -> list[str]:
    program = Program.objects.filter(pk=program_id).first()
    if program is None:
        return [f"{items[0]['label']}: программа удалена."]
    program = manual.lock_program(program)
    manual.lock_resources(program)
    problems = []
    for item in items:
        day = date.fromisoformat(item["date"])
        where = f"{item['label']}, {day:%d.%m}"
        old = item["before"]
        instructor = Instructor.objects.filter(pk=old["instructor_id"], is_active=True).first()
        prescription = program.prescriptions.filter(pk=item["prescription_id"]).first()
        if instructor is None or prescription is None:
            problems.append(f"{where}: инструктора или назначения больше нет.")
            continue
        current = None
        if item["after"] is not None:
            current = program.bookings.filter(
                prescription=prescription,
                date=day,
                instructor_id=item["after"]["instructor_id"],
                slot_id=item["after"]["slot_id"],
            ).first()
            if current is None:
                problems.append(f"{where}: занятие уже меняли после перестройки.")
                continue
        slot = InstructorSlot.objects.get(pk=old["slot_id"])
        violations = manual.check(
            program, prescription, day, slot.start, slot.end,
            moved=current, instructor=instructor, slot=slot,
        )  # fmt: skip
        hard, soft = manual.split(violations)
        # Инструктор всё ещё не в смене — возвращать некуда (для ручной правки это лишь
        # подтверждение, для возврата — препятствие).
        hard += [v for v in soft if v.code == ViolationCode.INSTRUCTOR_OFF_SHIFT]
        if hard:
            problems.append(f"{where}: {' '.join(v.message for v in hard)}")
            continue
        booking = current or Booking(
            program=program,
            prescription=prescription,
            procedure=prescription.procedure,
            kind=BookingKind.INDIVIDUAL,
            date=day,
        )
        booking.instructor, booking.slot = instructor, slot
        booking.start, booking.end = slot.start, slot.end
        booking.pinned = item["pinned"]
        if booking.pk:
            booking.version += 1
        booking._history_user = user
        booking._change_reason = "вернуть как было"
        booking.save()
    replan(program)
    return problems


# --- Точки входа: смена и распорядок изменились ---------------------------------------------


def after_shift_toggle(user: User, instructor: Instructor, day: date) -> StaffChange | None:
    """Клик в «Сменах» или «не работает» в шахматке (FR-STF-6)."""
    return rebuild(user, instructor, day, day, f"{instructor.short_name}: {day:%d.%m} не работает")


def set_not_working(user: User, instructor: Instructor, day: date) -> StaffChange | None:
    """«Не работает» из шахматки одним действием: исключение смены и перестройка."""
    staff_services.set_working(user, instructor, day, False)
    return after_shift_toggle(user, instructor, day)


def after_block(user: User, block: InstructorBlock) -> StaffChange | None:
    return rebuild(
        user,
        block.instructor,
        block.date,
        block.date,
        f"{block.instructor.short_name}: {block.date:%d.%m} {block.slot} — {block.text}",
    )


def after_duty(user: User, duty: InstructorDuty) -> StaffChange | None:
    return rebuild(
        user,
        duty.instructor,
        duty.valid_from,
        duty.valid_to,
        f"{duty.instructor.short_name}: распорядок {duty.slot} — {duty.text}",
    )


def after_pattern(user: User, instructor: Instructor, valid_from: date) -> StaffChange | None:
    pattern = instructor.shift_patterns.filter(valid_from=valid_from).first() or ShiftPattern(
        valid_from=valid_from
    )
    return rebuild(
        user,
        instructor,
        valid_from,
        None,
        f"{instructor.short_name}: смена с {valid_from:%d.%m} ({pattern.get_pattern_display()})"
        if pattern.pk
        else f"{instructor.short_name}: смена с {valid_from:%d.%m}",
    )
