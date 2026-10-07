"""Какие изменения моделей что обновляют на экранах (TZ.md NFR-11). Пакетные записи подбора
(``bulk_create``, ``_raw_delete``) сигналов не дают — ``replan`` публикует темы сам."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.programs.models import Prescription, Program, Withdrawal
from apps.scheduling.models import BoardDay, BoardPatient, Booking, BookingKind
from apps.staff.models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)

from . import topics
from .events import publish

# Что видно в шахматке: индивидуальные в сетке и записи на тренажёры справа.
ON_BOARD = (BookingKind.INDIVIDUAL, BookingKind.EQUIPMENT)


@receiver([post_save, post_delete], sender=Booking)
def booking_changed(instance: Booking, **kwargs) -> None:
    names = [topics.program(instance.program_id)]
    if instance.kind in ON_BOARD:
        names.append(topics.board(instance.date))
    publish(*names)


@receiver([post_save, post_delete], sender=BoardPatient)
def board_patient_changed(instance: BoardPatient, **kwargs) -> None:
    publish(topics.board(instance.date), topics.program(instance.program_id))


@receiver([post_save, post_delete], sender=BoardDay)
def board_day_changed(instance: BoardDay, **kwargs) -> None:
    publish(topics.board(instance.date))


@receiver([post_save, post_delete], sender=Program)
def program_changed(instance: Program, **kwargs) -> None:
    publish(topics.program(instance.pk), topics.PROGRAMS)


@receiver([post_save, post_delete], sender=Prescription)
@receiver([post_save, post_delete], sender=Withdrawal)
def program_part_changed(instance: Prescription | Withdrawal, **kwargs) -> None:
    publish(topics.program(instance.program_id), topics.PROGRAMS)


@receiver([post_save, post_delete], sender=Instructor)
@receiver([post_save, post_delete], sender=InstructorBlock)
@receiver([post_save, post_delete], sender=InstructorDuty)
@receiver([post_save, post_delete], sender=ShiftException)
@receiver([post_save, post_delete], sender=ShiftPattern)
def staff_changed(**kwargs) -> None:
    # Смена или распорядок меняют серые ячейки и колонки во всех шахматках.
    publish(topics.BOARD)
