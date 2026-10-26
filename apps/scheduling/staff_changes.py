"""После изменения смены или распорядка инструктора (TZ.md FR-STF-6, FR-SCH-11).

Инструктор заболел, получил блок или новую обязанность — его пациенты в составленных
шахматках, которых теперь нельзя вести, уходят в «Не распределены»; специалист ставит их сам.
Автоматической перестройки со сводкой и «Вернуть как было» больше нет (TZ.md §7.5).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q

from apps.accounts.models import User
from apps.catalog.models import GroupSession, InstructorSlot
from apps.programs.models import Program
from apps.staff import services as staff_services
from apps.staff.models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)

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
            # Порядок блокировок общий: программы → тренажёры → инструкторы → шахматки. Без
            # блокировки программ запись шахматки (FOR KEY SHARE на программу) ждала бы
            # пересборку, а та — блокировку шахматок, которую держим мы.
            programs = list(
                Program.objects.select_for_update(of=("self",))
                .filter(pk__in=ids)
                .select_related("department")
                .order_by("pk")
            )
            board_days.resync(programs, day)


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


# --- Инструктор в архиве (TZ.md FR-STF-1, решение 83) ---------------------------------------


@dataclass(frozen=True)
class InstructorRemoved:
    """Итог «Удалить» инструктора: совсем или в архив и что изменилось."""

    archived: bool
    partner: str = ""  # пара 2/2 разорвана — напарник остаётся один
    ended: int = 0  # закончено записей распорядка и шаблонов смены
    displaced: list = field(default_factory=list)  # пациенты в «Не распределены»


def instructor_is_used(instructor: Instructor) -> bool:
    """Смены, исключения, распорядок, разовые блоки, занятия в шахматке."""
    return (
        ShiftPattern.objects.filter(instructor=instructor).exists()
        or ShiftException.objects.filter(instructor=instructor).exists()
        or InstructorDuty.objects.filter(instructor=instructor).exists()
        or InstructorBlock.objects.filter(instructor=instructor).exists()
        or Booking.objects.filter(instructor=instructor).exists()
    )


def delete_instructor(user: User, instructor: Instructor) -> InstructorRemoved:
    """«Удалить» инструктора. Ни разу не работавший удаляется совсем. Иначе — в архив: с
    сегодняшнего дня его нет в сменах и шахматке, шаблон смены и распорядок заканчиваются
    сегодня, будущие исключения и разовые блоки удаляются, пара 2/2 разрывается; его
    пациенты в составленных шахматках уходят в «Не распределены» — как после изменения смены.

    Порядок блокировок: сначала транзакция с инструкторами, потом — как у «Смен» — сверка
    шахматок (программы → шахматки) отдельными транзакциями."""
    from .services import watch_displaced

    staff_services.require_instructors(user)
    if not instructor.is_active:
        raise staff_services.StaffError("Инструктор уже в архиве.")
    today = board_days.today()
    removed = _archive_or_delete(user, instructor, today)
    if not removed.archived:
        return removed
    with watch_displaced() as displaced:
        rebuild(instructor, today, None)
    return InstructorRemoved(True, removed.partner, removed.ended, displaced)


@transaction.atomic
def _archive_or_delete(user: User, instructor: Instructor, today: date) -> InstructorRemoved:
    partner = instructor.partner
    if not instructor_is_used(instructor):
        staff_services.set_partner(instructor, None, user)
        locked = Instructor.objects.select_for_update().get(pk=instructor.pk)
        if instructor_is_used(locked):
            raise staff_services.StaffError(
                "Инструктора только что поставили в смену или расписание — обновите страницу."
            )
        locked._history_user = user
        locked.delete()
        return InstructorRemoved(False, partner.short_name if partner else "")
    # Пара разрывается тем же сервисом, что на экране: блокировка по pk, журнал.
    staff_services.set_partner(instructor, None, user)
    instructor = Instructor.objects.select_for_update().get(pk=instructor.pk)
    instructor.is_active = False
    instructor._history_user = user
    instructor.save(update_fields=["is_active"])
    ended = 0
    for model in (ShiftPattern, InstructorDuty):
        for item in model.objects.filter(instructor=instructor).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gt=today)
        ):
            item._history_user = user
            if item.valid_from > today:
                item.delete()
            else:
                item.valid_to = today
                item.save(update_fields=["valid_to"])
            ended += 1
    for model in (ShiftException, InstructorBlock):
        for item in model.objects.filter(instructor=instructor, date__gt=today):
            item._history_user = user
            item.delete()
    return InstructorRemoved(True, partner.short_name if partner else "", ended)


@transaction.atomic
def restore_instructor(user: User, instructor: Instructor) -> Instructor:
    """«Вернуть из архива»: инструктор снова действует, но без смены — шаблон смены задают
    на экране «Смены», напарника — в его окне."""
    staff_services.require_instructors(user)
    instructor = Instructor.objects.select_for_update().get(pk=instructor.pk)
    instructor.is_active = True
    instructor._history_user = user
    instructor.save(update_fields=["is_active"])
    # Архив закончил смену и распорядок сегодняшним днём включительно: при возврате в тот же
    # день они снова действовали бы сегодня, а инструктор возвращается без смены (FR-STF-1).
    today = board_days.today()
    for model in (ShiftPattern, InstructorDuty):
        for item in model.objects.filter(instructor=instructor, valid_to=today):
            item._history_user = user
            if item.valid_from >= today:
                item.delete()
            else:
                item.valid_to = today - timedelta(days=1)
                item.save(update_fields=["valid_to"])
    return instructor
