"""Инструкторы: пары, смены, распорядок и разовые блоки (TZ.md §4.6).

Смены и распорядок — ресурс центра: правят специалист ФР любого отделения и администратор
(решение 46, 03-architecture.md §8), врач — нет. Права проверяются здесь, а не в представлениях.
Все изменения сохраняются через save() с ``_history_user`` — так автор попадает в журнал (NFR-5).
"""

import calendar
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet

from apps.accounts.access import has_role_anywhere
from apps.accounts.models import Role, User
from apps.catalog.models import InstructorSlot

from . import domain
from .models import (
    Instructor,
    InstructorBlock,
    InstructorDuty,
    ShiftException,
    ShiftPattern,
)


class StaffError(Exception):
    """Ошибка правки смен или распорядка — текст показывается пользователю."""


# --- Пары напарников ------------------------------------------------------------------------


@transaction.atomic
def set_partner(instructor: Instructor, partner: Instructor | None) -> None:
    """Связь напарников симметрична (FR-STF-1): A↔B. Прежние пары обоих разрываются.

    Сохраняем через save(), а не update(), чтобы каждое изменение попало в журнал (NFR-5).
    """
    if partner is not None and partner.pk == instructor.pk:
        raise ValueError("Инструктор не может быть напарником сам себе.")
    pks = {instructor.pk} | ({partner.pk} if partner is not None else set())
    _link(_lock_pair(pks), instructor, partner)
    instructor.refresh_from_db(fields=["partner"])
    if partner is not None:
        partner.refresh_from_db(fields=["partner"])


@transaction.atomic
def save_instructor(instructor: Instructor, partner: Instructor | None, *, relink: bool) -> None:
    """Сохранить инструктора из админки (в том числе порядок в списке): участники пары
    блокируются одним запросом до первого изменения — тот же порядок «по pk», что у подбора
    расписания, иначе правка пары и подбор могли бы ждать друг друга (03-architecture.md §4.5).
    ``relink`` — напарника меняли: пару ставит ``set_partner``.
    """
    if partner is not None and partner.pk == instructor.pk:
        raise ValueError("Инструктор не может быть напарником сам себе.")
    pks = {pk for pk in (instructor.pk, partner.pk if partner else None) if pk is not None}
    locked = _lock_pair(pks) if pks else {}
    # Пару меняет только _link: здесь — прежнее значение из базы (или пусто для нового).
    instructor.partner_id = locked[instructor.pk].partner_id if instructor.pk in locked else None
    instructor.save()
    if relink:
        locked[instructor.pk] = instructor
        _link(locked, instructor, partner)


def _lock_pair(pks: set[int]) -> dict[int, Instructor]:
    """Блокирует участников и их прежних напарников одним запросом, по pk.

    Напарников читаем без блокировки, а после блокировки проверяем: если пару за это время
    поменяли, — понятная ошибка вместо полупары.
    """
    partners = set(
        Instructor.objects.filter(pk__in=pks, partner__isnull=False).values_list(
            "partner_id", flat=True
        )
    )
    wanted = pks | partners
    locked = {
        i.pk: i for i in Instructor.objects.select_for_update().filter(pk__in=wanted).order_by("pk")
    }
    if any(locked[pk].partner_id not in (None, *wanted) for pk in pks if pk in locked):
        raise StaffError("Пару этого инструктора только что изменили. Повторите сохранение.")
    return locked


def _link(
    locked: dict[int, Instructor], instructor: Instructor, partner: Instructor | None
) -> None:
    target = {instructor.pk: partner.pk} if partner is not None else {}
    if partner is not None:
        target[partner.pk] = instructor.pk
    # Сначала разрываем старые связи — иначе уникальность partner мешает переназначению.
    for item in locked.values():
        if item.partner_id is not None and item.partner_id != target.get(item.pk):
            item.partner = None
            item.save(update_fields=["partner"])
    for pk, partner_pk in target.items():
        item = locked[pk]
        if item.partner_id != partner_pk:
            item.partner_id = partner_pk
            item.save(update_fields=["partner"])


# --- Права ----------------------------------------------------------------------------------


def can_manage_staff(user: User) -> bool:
    """Смены и распорядок правят специалист ФР (в любом отделении) и администратор."""
    return has_role_anywhere(user, Role.REHAB)


def require_staff_manager(user: User) -> None:
    if not can_manage_staff(user):
        raise PermissionDenied("Смены и распорядок инструкторов правит специалист ФР.")


def active_instructors() -> list[Instructor]:
    """Действующие инструкторы по порядку шахматки, напарники 2/2 — рядом (FR-STF-4)."""
    instructors = list(Instructor.objects.filter(is_active=True).select_related("partner"))
    result: list[Instructor] = []
    placed: set[int] = set()
    for item in instructors:
        if item.pk in placed:
            continue
        result.append(item)
        placed.add(item.pk)
        partner = item.partner
        if partner is not None and partner.is_active and partner.pk not in placed:
            result.append(partner)
            placed.add(partner.pk)
    return result


# --- Данные для домена: календари инструкторов ---------------------------------------------


def instructor_calendars(
    instructors: Iterable[Instructor] | None, start: date, end: date
) -> dict[int, domain.InstructorCalendar]:
    """Календари инструкторов на даты ``start…end`` — вход для домена (FR-STF-2…5).

    Пять запросов на всех инструкторов сразу (и сетка слотов — для групп длиннее слота).
    Календарь помнит ``start…end``: вопрос о дате вне диапазона — ``ValueError``.
    ``None`` — все действующие. Подбор (шаг 3.2) и шахматка (шаг 3.3) задают вопросы «в дату»
    уже к результату::

        calendars = instructor_calendars(None, day, day)
        calendars[instructor.pk].day(day).is_free(slot.pk)
    """
    if instructors is None:
        instructors = Instructor.objects.filter(is_active=True)
    ids = [item.pk for item in instructors]
    period = Q(valid_from__lte=end) & (Q(valid_to__isnull=True) | Q(valid_to__gte=start))
    parts: dict[str, defaultdict[int, list]] = {
        name: defaultdict(list) for name in ("patterns", "exceptions", "duties", "blocks")
    }
    for item in ShiftPattern.objects.filter(period, instructor_id__in=ids):
        parts["patterns"][item.instructor_id].append(item.to_domain())
    for item in ShiftException.objects.filter(instructor_id__in=ids, date__range=(start, end)):
        parts["exceptions"][item.instructor_id].append(item.to_domain())
    duties = InstructorDuty.objects.filter(period, instructor_id__in=ids)
    for item in duties.select_related("group_session__procedure"):
        parts["duties"][item.instructor_id].append(item.to_domain())
    blocks = InstructorBlock.objects.filter(instructor_id__in=ids, date__range=(start, end))
    for item in blocks.select_related("group_session__procedure"):
        parts["blocks"][item.instructor_id].append(item.to_domain())
    grid = tuple(
        domain.Slot(slot.pk, slot.start, slot.end) for slot in InstructorSlot.objects.all()
    )
    return {
        pk: domain.InstructorCalendar(
            patterns=tuple(parts["patterns"][pk]),
            exceptions=tuple(parts["exceptions"][pk]),
            duties=tuple(parts["duties"][pk]),
            blocks=tuple(parts["blocks"][pk]),
            slots=grid,
            start=start,
            end=end,
        )
        for pk in ids
    }


def build_teams(instructors: Iterable[Instructor] | None = None) -> list[domain.Team]:
    """Команды шахматки из действующих инструкторов: пара 2/2 — одна команда (§7.3).

    Напарник, который не действует, в команду не входит — инструктор остаётся одиночкой.
    """
    items = active_instructors() if instructors is None else list(instructors)
    active = {item.pk for item in items}
    teams: list[domain.Team] = []
    seen: set[int] = set()
    for item in items:
        if item.pk in seen:
            continue
        members = [item]
        partner = item.partner
        if partner is not None and partner.pk in active and partner.pk not in seen:
            members.append(partner)
        seen.update(member.pk for member in members)
        teams.append(
            domain.Team.of(
                *(domain.TeamMember(m.pk, m.short_name, m.display_order) for m in members)
            )
        )
    return teams


# --- Смены (FR-STF-2…4) --------------------------------------------------------------------


@dataclass(frozen=True)
class ShiftCell:
    day: date
    working: bool
    exception: domain.ShiftOverride | None


@dataclass(frozen=True)
class ShiftRow:
    instructor: Instructor
    pattern: ShiftPattern | None
    cells: list[ShiftCell]


def month_days(year: int, month: int) -> list[date]:
    return [date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)]


def shift_month(user: User, year: int, month: int) -> list[ShiftRow]:
    """Календарь смен на месяц: строки — инструкторы, ячейки — рабочие дни (FR-STF-4)."""
    require_staff_manager(user)
    days = month_days(year, month)
    instructors = active_instructors()
    calendars = instructor_calendars(instructors, days[0], days[-1])
    patterns = _patterns_shown(instructors, days[0], days[-1])
    return [
        ShiftRow(
            instructor=item,
            pattern=patterns.get(item.pk),
            cells=[
                ShiftCell(
                    day,
                    calendars[item.pk].is_working(day),
                    calendars[item.pk].exception_on(day),
                )
                for day in days
            ],
        )
        for item in instructors
    ]


def _patterns_shown(
    instructors: list[Instructor], start: date, end: date
) -> dict[int, ShiftPattern]:
    """Шаблон рядом с инструктором — последний из действующих в месяце."""
    found = ShiftPattern.objects.filter(
        Q(valid_from__lte=end) & (Q(valid_to__isnull=True) | Q(valid_to__gte=start)),
        instructor__in=instructors,
    ).order_by("valid_from")
    return {item.instructor_id: item for item in found}


def current_pattern(instructor: Instructor, day: date) -> ShiftPattern | None:
    return (
        instructor.shift_patterns.filter(valid_from__lte=day)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day))
        .first()
    )


@transaction.atomic
def change_shift_pattern(
    user: User, instructor: Instructor, pattern: str, anchor_date: date, valid_from: date
) -> ShiftPattern:
    """Задать или сменить шаблон смены с даты (FR-STF-2).

    Действующий в эту дату шаблон заканчивается накануне, новый действует бессрочно. Шаблон,
    начатый ровно с этой даты, правится на месте (исправление ошибки). Если позже уже есть
    шаблон — ошибка: система не удаляет периоды молча, новый шаблон задают с более поздней даты.
    """
    require_staff_manager(user)
    # Блокировка строки инструктора: две параллельные смены шаблона не создадут пересечение.
    Instructor.objects.select_for_update().get(pk=instructor.pk)
    later = instructor.shift_patterns.filter(valid_from__gt=valid_from).first()
    if later is not None:
        raise StaffError(
            f"С {later.valid_from:%d.%m.%Y} уже задан другой шаблон — "
            "задайте новый с этой даты или позже."
        )
    same = instructor.shift_patterns.filter(valid_from=valid_from).first()
    if same is not None:
        same.pattern, same.anchor_date = pattern, anchor_date
        result = _save(user, same)
    else:
        previous = current_pattern(instructor, valid_from)
        if previous is not None:
            previous.valid_to = valid_from - timedelta(days=1)
            _save(user, previous)
        result = _save(
            user,
            ShiftPattern(
                instructor=instructor,
                pattern=pattern,
                anchor_date=anchor_date,
                valid_from=valid_from,
            ),
        )
    _drop_redundant_exceptions(user, result)
    return result


def _drop_redundant_exceptions(user: User, pattern: ShiftPattern) -> None:
    """Исключения, совпавшие с новым шаблоном, больше не отступления — удаляем (решение 51).

    Только без причины (клик в календаре): «больничный» или «подмена» остаются как запись
    о событии, даже если по новому шаблону день и так такой же.
    """
    rule = pattern.to_domain()
    candidates = ShiftException.objects.filter(
        instructor_id=pattern.instructor_id, date__gte=pattern.valid_from, reason=""
    )
    if pattern.valid_to is not None:
        candidates = candidates.filter(date__lte=pattern.valid_to)
    for item in candidates:
        if item.is_working == rule.works(item.date):
            item._history_user = user
            item.delete()


@transaction.atomic
def set_working(
    user: User, instructor: Instructor, day: date, is_working: bool, reason: str = ""
) -> ShiftException | None:
    """Отметить, работает ли инструктор в дату (FR-STF-3, FR-STF-6).

    Если отметка совпадает с шаблоном, исключение не нужно и удаляется — в календаре остаются
    только настоящие отступления от графика. Возвращает исключение или None.

    Перестройку его индивидуальных занятий со сводкой (FR-SCH-13, решение 43) запускает
    ``apps.scheduling.staff_changes`` после этой транзакции — расписание зависит от смен, а не
    наоборот.
    """
    require_staff_manager(user)
    Instructor.objects.select_for_update().get(pk=instructor.pk)
    by_pattern = instructor_calendars([instructor], day, day)[instructor.pk].by_pattern(day)
    existing = ShiftException.objects.filter(instructor=instructor, date=day).first()
    if is_working == by_pattern:
        if existing is not None:
            existing._history_user = user
            existing.delete()
        return None
    item = existing or ShiftException(instructor=instructor, date=day)
    item.is_working, item.reason = is_working, reason
    return _save(user, item)


@transaction.atomic
def toggle_shift_day(user: User, instructor: Instructor, day: date) -> ShiftException | None:
    """Клик по ячейке «Смен»: рабочий день становится нерабочим и наоборот (FR-STF-4).

    Повторный клик возвращает день к шаблону — исключение удаляется. Состояние читается после
    блокировки инструктора: двойной клик не превратится в два одинаковых переключения.
    """
    require_staff_manager(user)
    Instructor.objects.select_for_update().get(pk=instructor.pk)
    working = instructor_calendars([instructor], day, day)[instructor.pk].is_working(day)
    return set_working(user, instructor, day, not working)


# --- Распорядок и разовые блоки (FR-STF-5) -------------------------------------------------


@transaction.atomic
def save_duty(user: User, duty: InstructorDuty) -> InstructorDuty:
    """Добавить или изменить постоянную обязанность инструктора."""
    require_staff_manager(user)
    Instructor.objects.select_for_update().get(pk=duty.instructor_id)
    return _save(user, duty)


@transaction.atomic
def end_duty(user: User, duty: InstructorDuty, valid_to: date) -> InstructorDuty:
    """Завершить обязанность: последний день, когда она действует."""
    require_staff_manager(user)
    if valid_to < duty.valid_from:
        raise StaffError(
            f"Обязанность действует с {duty.valid_from:%d.%m.%Y} — завершить раньше нельзя."
        )
    duty.valid_to = valid_to
    return _save(user, duty)


@transaction.atomic
def save_block(user: User, block: InstructorBlock) -> InstructorBlock:
    """Разовый блок на дату: другая обязанность в слоте или «снять распорядок»."""
    require_staff_manager(user)
    Instructor.objects.select_for_update().get(pk=block.instructor_id)
    existing = InstructorBlock.objects.filter(
        instructor_id=block.instructor_id, date=block.date, slot_id=block.slot_id
    ).exclude(pk=block.pk)
    if existing.exists():
        raise StaffError(f"На {block.date:%d.%m.%Y} в слоте {block.slot} уже есть разовый блок.")
    return _save(user, block)


@transaction.atomic
def delete_block(user: User, block: InstructorBlock) -> None:
    """Удалить разовый блок — день снова идёт по распорядку. Удаление попадает в журнал."""
    require_staff_manager(user)
    block._history_user = user
    block.delete()


def duties_of(instructor: Instructor) -> QuerySet[InstructorDuty]:
    return instructor.duties.select_related("slot", "group_session__procedure").order_by(
        "slot__start", "valid_from"
    )


def upcoming_blocks(instructor: Instructor, today: date) -> QuerySet[InstructorBlock]:
    return instructor.blocks.filter(date__gte=today).select_related(
        "slot", "group_session__procedure"
    )


def _save[T: (ShiftPattern, ShiftException, InstructorDuty, InstructorBlock)](
    user: User, item: T
) -> T:
    item.full_clean()
    item._history_user = user
    item.save()
    return item
