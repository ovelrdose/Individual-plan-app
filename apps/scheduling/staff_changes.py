"""После изменения смены или распорядка инструктора (TZ.md FR-STF-6, FR-SCH-11).

Инструктор заболел, получил блок или новую обязанность — его пациенты в составленных
шахматках, которых теперь нельзя вести, уходят в «Не распределены»; специалист ставит их сам.
Автоматической перестройки со сводкой и «Вернуть как было» больше нет (TZ.md §7.5).
"""

from collections import defaultdict
from datetime import date

from django.db import transaction

from apps.accounts.models import User
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
