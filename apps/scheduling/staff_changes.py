"""После изменения смены или распорядка инструктора (TZ.md FR-STF-6, FR-SCH-11).

Инструктор заболел, получил блок или новую обязанность — его пациенты в составленных
шахматках, которых теперь нельзя вести, уходят в «Не распределены»; специалист ставит их сам.
Автоматической перестройки со сводкой и «Вернуть как было» больше нет (TZ.md §7.5).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.db.models import Q

from apps.accounts.models import User
from apps.catalog.models import GroupSession, InstructorSlot
from apps.programs.models import Program
from apps.staff import services as staff_services
from apps.staff.models import Instructor, InstructorBlock, InstructorDuty

from . import board_days
from .models import Booking, BookingKind


def rebuild(instructor: Instructor, start: date, end: date | None) -> None:
    """Смена или занятость инструктора изменилась в ``start…end`` (``None`` — без конца):
    его пациенты в составленных шахматках, которых теперь нельзя вести, уходят в
    «Не распределены» (FR-STF-6, FR-SCH-11)."""
    bookings = Booking.objects.filter(
        kind=BookingKind.INDIVIDUAL, instructor=instructor, date__gte=max(start, board_days.today())
    )
    if end is not None:
        bookings = bookings.filter(date__lte=end)
    by_day: dict[date, set[int]] = defaultdict(set)
    for day, program_id in bookings.values_list("date", "program_id"):
        by_day[day].add(program_id)
    for day, ids in sorted(by_day.items()):
        with transaction.atomic():
            board_days.resync(Program.objects.filter(pk__in=ids).select_related("department"), day)


# --- Точки входа: смена и распорядок изменились ---------------------------------------------


def after_shift_toggle(user: User, instructor: Instructor, day: date) -> None:
    """Клик в «Сменах» или «не работает» в шахматке (FR-STF-6)."""
    rebuild(instructor, day, day)


def set_not_working(user: User, instructor: Instructor, day: date) -> None:
    """«Не работает» из шахматки одним действием: исключение смены и «Не распределены»."""
    staff_services.set_working(user, instructor, day, False)
    after_shift_toggle(user, instructor, day)


def after_block(user: User, block: InstructorBlock) -> None:
    rebuild(block.instructor, block.date, block.date)


def after_duty(user: User, duty: InstructorDuty) -> None:
    rebuild(duty.instructor, duty.valid_from, duty.valid_to)


def after_pattern(user: User, instructor: Instructor, valid_from: date) -> None:
    rebuild(instructor, valid_from, None)


# --- Группа больше не идёт в слоте инструктора (решение 82) ----------------------------------


@dataclass(frozen=True)
class FreedLead:
    """Инструктор, у которого закончилось «Ведение группы»: группа или её занятие выключены
    или занятие ушло из его слота. Слот с завтрашнего дня свободен."""

    instructor: str
    slot: InstructorSlot
    group: str

    def __str__(self) -> str:
        return f"{self.instructor} {self.slot.start:%-H:%M} ({self.group})"


def group_lead_instructors(session_ids: list[int]) -> list[int]:
    """Инструкторы, у которых есть распорядок или разовый блок «Ведение группы» на эти
    занятия."""
    ids = set(
        InstructorDuty.objects.filter(group_session__in=session_ids).values_list(
            "instructor", flat=True
        )
    ) | set(
        InstructorBlock.objects.filter(group_session__in=session_ids).values_list(
            "instructor", flat=True
        )
    )
    return sorted(ids)


def end_group_leads(user: User, session_ids: list[int], today: date) -> list[FreedLead]:
    """Ведение выключенных или перенесённых занятий групп заканчивается сегодняшним днём
    (решение 82): постоянный распорядок — по сегодня (начинавшийся позже — удаляется), разовые
    блоки на будущие даты удаляются. Прошлое остаётся. Распорядок, который и после переноса
    пересекается со своим слотом, не трогается.

    Освобождённый слот ничьих занятий не ломает, поэтому шахматки не пересчитываются:
    пациентов из «Не распределены» специалист ставит в свободный слот сам."""
    freed: list[FreedLead] = []
    sessions = GroupSession.objects.select_related("procedure").in_bulk(session_ids)
    duties = (
        InstructorDuty.objects.filter(group_session__in=session_ids)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=today))
        .select_related("instructor", "slot")
        .order_by("instructor__display_order", "slot__start", "pk")
    )
    for duty in duties:
        session = sessions[duty.group_session_id]
        if _still_led(session, duty.slot):
            continue
        duty._history_user = user
        if duty.valid_from > today:
            duty.delete()
        else:
            duty.valid_to = today
            duty.save(update_fields=["valid_to"])
        freed.append(FreedLead(duty.instructor.short_name, duty.slot, session.procedure.name))
    blocks = InstructorBlock.objects.filter(
        group_session__in=session_ids, date__gt=today
    ).select_related("instructor", "slot")
    for block in blocks:
        if _still_led(sessions[block.group_session_id], block.slot):
            continue
        block._history_user = user
        block.delete()
    return freed


def _still_led(session: GroupSession, slot: InstructorSlot) -> bool:
    return session.is_active and session.start_time < slot.end and slot.start < session.end_time
