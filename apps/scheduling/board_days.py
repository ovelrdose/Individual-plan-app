"""Шахматка на день вперёд (TZ.md §7.2–7.3, FR-SCH-5…13): создание, перенос, синхронизация.

Шахматка на сегодня и на следующий будний день есть всегда: ``ensure_boards`` создаёт
недостающую копированием предыдущей составленной (выписанные уходят, остальные остаются на
своих местах, новые раскладываются сами). Отдельной кнопки и задания на сервере нет —
создание происходит при первом обращении к расписанию в новый день.

Расчёт дня — в домене (``domain.board_day.plan_day``), здесь — данные и запись. Все
изменения шахматок идут под одной транзакционной блокировкой: два пользователя не составят
один день дважды и не поставят пациента параллельно в две ячейки.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, time, timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalog.domain import add_minutes, minutes_between
from apps.catalog.models import InstructorSlot, ProcedureKind
from apps.programs.domain import room_sort_key
from apps.programs.models import Prescription, Program, withdrawn_on
from apps.staff.domain import Pattern
from apps.staff.models import Instructor
from apps.staff.services import build_teams, instructor_calendars

from .domain.board_day import Column, GridSlot, Member, Patient, PatientDay, Seat, plan_day
from .domain.model import is_weekend
from .models import BoardDay, BoardPatient, Booking, BookingKind, BookingSource

# Ключ транзакционной блокировки PostgreSQL для всех изменений шахматок.
BOARD_LOCK = 4_041_001
MIDNIGHT = time(0, 0)


def minutes(value: time) -> int:
    return minutes_between(MIDNIGHT, value)


def today() -> date:
    return timezone.localdate()


def next_weekday(day: date) -> date:
    """Следующий будний день: после пятницы — понедельник (FR-SCH-3)."""
    day += timedelta(days=1)
    while is_weekend(day):
        day += timedelta(days=1)
    return day


def board_dates(current: date) -> list[date]:
    """Какие шахматки должны быть составлены: сегодня (в будни) и следующий будний день."""
    dates = [] if is_weekend(current) else [current]
    return [*dates, next_weekday(current)]


def editable(day: date, current: date | None = None) -> bool:
    """Править можно сегодняшнюю и завтрашнюю шахматку, прошедшие — только смотреть."""
    return day in board_dates(current or today()) and BoardDay.objects.filter(date=day).exists()


def lock() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BOARD_LOCK])


def ensure_boards(current: date | None = None) -> None:
    """Составляет недостающие шахматки на сегодня и следующий будний день (FR-SCH-5)."""
    current = current or today()
    dates = board_dates(current)
    if BoardDay.objects.filter(date__in=dates).count() == len(dates):
        return
    for day in dates:
        create_board(day)
    drop_beyond_boards()


@transaction.atomic
def drop_beyond_boards() -> int:
    """Индивидуальные живут только в составленных шахматках. Всё, что позже последней (прототип
    подбирал их на весь курс), — не расписание: удаляется."""
    lock()
    last = BoardDay.objects.order_by("-date").values_list("date", flat=True).first()
    if last is None:
        return 0
    deleted, _ = Booking.objects.filter(kind=BookingKind.INDIVIDUAL, date__gt=last).delete()
    return deleted


@transaction.atomic
def create_board(day: date) -> bool:
    """Шахматка на дату из предыдущей составленной (FR-SCH-6); первой нет — все пациенты
    раскладываются как новые (FR-SCH-13). Уже составлена — ничего не делает."""
    lock()
    if BoardDay.objects.filter(date=day).exists():
        return False
    source = BoardDay.objects.filter(date__lt=day).order_by("-date").first()
    BoardDay.objects.create(date=day)
    # Индивидуальные, поставленные на эту дату раньше (прототип подбирал их на весь курс),
    # шахматкой не являются — день составляется заново.
    Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL).delete()
    BoardPatient.objects.filter(date=day).delete()
    programs = _programs_on(day)
    _recompute(day, programs, source=source.date if source else None, auto=True)
    return True


def sync_program(program: Program, seats: dict[date, list[Seat]] | None = None) -> None:
    """Привести пациента в составленных шахматках (сегодня и дальше) в соответствие с его
    назначениями и днями курса: лишние занятия убираются, ставшие невозможными — в
    «Не распределены», новые — в завтрашнюю шахматку автоматически (FR-SCH-7, FR-SCH-9).

    ``seats`` — где пациент стоял до пересборки групп (``replan`` снимает его индивидуальные
    на время пересборки, чтобы группы встали по своему расписанию).
    """
    current = today()
    ensure_boards(current)
    lock()
    for day in BoardDay.objects.filter(date__gte=current).values_list("date", flat=True):
        overrides = {program.pk: seats.get(day, [])} if seats is not None else None
        _recompute(day, [program], auto=day > current, seats=overrides)


def resync(programs: Iterable[Program], day: date) -> None:
    """Проверить пациентов на дату после изменения смены или занятости инструктора: занятия,
    которые стали невозможны, уходят в «Не распределены» (FR-STF-6)."""
    if not BoardDay.objects.filter(date=day).exists():
        return
    lock()
    _recompute(day, list(programs), auto=False)


def propagate(program: Program, day: date) -> None:
    """Правка сегодняшней шахматки переходит на завтрашнюю, если там пациента отдельно не
    правили (FR-SCH-12). Правка завтрашней на сегодняшнюю не влияет."""
    current = today()
    if day != current:
        return
    following = next_weekday(day)
    if not BoardDay.objects.filter(date=following).exists():
        return
    if BoardPatient.objects.filter(date=following, program=program, edited=True).exists():
        return
    _recompute(following, [program], source=day, auto=True)


def mark_edited(program: Program, day: date, *, unplaced: int = 0, cancelled: int = 0) -> None:
    """Пациента правили вручную в эту дату; изменить блоки на ``unplaced`` / ``cancelled``."""
    entry, _ = BoardPatient.objects.get_or_create(date=day, program=program)
    entry.unplaced = max(entry.unplaced + unplaced, 0)
    entry.cancelled = max(entry.cancelled + cancelled, 0)
    entry.edited = True
    entry.save()


# --- Расчёт и запись ------------------------------------------------------------------------


def _recompute(
    day: date,
    programs: list[Program],
    *,
    source: date | None = None,
    auto: bool,
    seats: dict[int, list[Seat]] | None = None,
) -> None:
    """Пересчитать пациентов ``programs`` на дату ``day``: перенести их положение с даты
    ``source`` (по умолчанию — с этой же даты) и записать результат."""
    if not programs:
        return
    ids = [p.pk for p in programs]
    origin = source or day
    slots = list(InstructorSlot.objects.order_by("start"))
    grid = [GridSlot(s.pk, minutes(s.start), minutes(s.end), s.is_evening) for s in slots]
    prescriptions = _individual_prescriptions(programs, day)
    previous = seats if seats is not None else _seats_on(origin, ids)
    holds = {
        item.program_id: item for item in BoardPatient.objects.filter(date=origin, program__in=ids)
    }
    busy = _busy_on(day, ids)
    patients = [
        Patient(
            program_id=program.pk,
            need=len(prescriptions[program.pk]),
            seats=tuple(previous.get(program.pk, ())),
            unplaced=holds[program.pk].unplaced if program.pk in holds else 0,
            cancelled=holds[program.pk].cancelled if program.pk in holds else 0,
            busy=tuple(busy[program.pk]),
            evening_note=_evening_note(program, day),
        )
        for program in sorted(programs, key=_patient_order)
    ]
    fixed = tuple(
        Seat(instructor_id, slot_id)
        for instructor_id, slot_id in Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL)
        .exclude(program__in=ids)
        .values_list("instructor_id", "slot_id")
    )
    result = plan_day(patients, _columns(day, grid), grid, fixed=fixed, auto=auto)
    by_slot = {s.pk: s for s in slots}
    for item in result:
        # Старые места пациента на этот день, которых нет в новом плане, убираем до сдвига
        # тренажёров: иначе тренажёр мог бы встать на место, которое вот-вот освободится, и
        # база не дала бы записать пациента в два места сразу.
        _drop_stale(day, item)
        # Тренажёры подбора уступают место индивидуальному; не сдвинуть — пациент в блок.
        seats = [
            (minutes(by_slot[s.slot_id].start), minutes(by_slot[s.slot_id].end)) for s in item.seats
        ]
        fits = fit_trainers(item.program_id, day, seats)
        if not all(fits):
            kept = tuple(seat for seat, ok in zip(item.seats, fits, strict=True) if ok)
            item = replace(item, seats=kept, unplaced=item.unplaced + len(item.seats) - len(kept))
        _write(day, item, prescriptions[item.program_id], by_slot)


def _drop_stale(day: date, item: PatientDay) -> None:
    wanted = {(s.instructor_id, s.slot_id, s.note) for s in item.seats}
    for booking in Booking.objects.filter(
        date=day, kind=BookingKind.INDIVIDUAL, program_id=item.program_id
    ):
        if (booking.instructor_id, booking.slot_id, booking.note) not in wanted:
            booking.delete()


def _movable_trainer() -> Q:
    return Q(kind=BookingKind.EQUIPMENT, pinned=False)


def fit_trainers(program_id: int, day: date, seats: list[tuple[int, int]]) -> list[bool]:
    """Освободить время индивидуальных пациента от его тренажёров, поставленных подбором:
    тренажёр сдвигается на ближайшее свободное время своего окна (вместимость, другие занятия
    пациента). Группы, бассейн и закреплённые вручную тренажёры не двигаются. Возвращает по
    каждому индивидуальному — нашлось ли ему место (сдвиги уже записаны)."""
    bookings = list(
        Booking.objects.filter(program_id=program_id, date=day)
        .exclude(kind=BookingKind.INDIVIDUAL)
        .select_related("equipment")
    )
    movable = [b for b in bookings if b.kind == BookingKind.EQUIPMENT and not b.pinned]
    fixed = [(minutes(b.start), minutes(b.end)) for b in bookings if b not in movable]
    taken: list[tuple[int, int]] = []
    result = []
    for start, end in seats:
        if _overlaps((start, end), fixed + taken):
            result.append(False)
            continue
        moves = _moves(day, movable, fixed + taken + [(start, end)])
        if moves is None:
            result.append(False)
            continue
        for booking, new_start in moves:
            booking.start = new_start
            booking.end = add_minutes(new_start, booking.equipment.duration_min)
            booking.save(update_fields=["start", "end"])
        taken.append((start, end))
        result.append(True)
    return result


def _moves(
    day: date, movable: list[Booking], busy: list[tuple[int, int]]
) -> list[tuple[Booking, time]] | None:
    """Куда сдвинуть тренажёры, которые попали на ``busy``; ``None`` — некуда."""
    moves: list[tuple[Booking, time]] = []
    placed = [(minutes(b.start), minutes(b.end)) for b in movable]
    for index, booking in enumerate(movable):
        current = placed[index]
        if not _overlaps(current, busy):
            continue
        others = busy + [p for i, p in enumerate(placed) if i != index]
        equipment = booking.equipment
        options = sorted(
            equipment.start_times(),
            key=lambda t: (abs(minutes(t) - current[0]), t),
        )
        for option in options:
            span = (minutes(option), minutes(option) + equipment.duration_min)
            if _overlaps(span, others):
                continue
            used = (
                Booking.objects.filter(
                    date=day, kind=BookingKind.EQUIPMENT, equipment=equipment, start=option
                )
                .exclude(pk=booking.pk)
                .count()
            )
            if used >= equipment.capacity:
                continue
            placed[index] = span
            moves.append((booking, option))
            break
        else:
            return None
    return moves


def _overlaps(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in others)


def _write(
    day: date,
    item: PatientDay,
    units: list[Prescription],
    slots: dict[int, InstructorSlot],
) -> None:
    """Записать пациента на дату: ячейки — занятиями, блоки — ``BoardPatient``. Ячейки, которые
    не изменились, не пересоздаются (журнал изменений не засоряется)."""
    existing = list(
        Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL, program_id=item.program_id)
    )
    wanted = sorted(item.seats, key=lambda s: slots[s.slot_id].start)
    keep: list[Booking] = []
    create: list[Booking] = []
    for index, seat in enumerate(wanted):
        prescription = units[min(index, len(units) - 1)]
        match = next(
            (
                b
                for b in existing
                if (b.instructor_id, b.slot_id, b.note)
                == (seat.instructor_id, seat.slot_id, seat.note)
            ),
            None,
        )
        if match is not None:
            existing.remove(match)
            if match.prescription_id != prescription.pk:
                match.prescription = prescription
                match.procedure_id = prescription.procedure_id
                match.save(update_fields=["prescription", "procedure"])
            keep.append(match)
            continue
        slot = slots[seat.slot_id]
        create.append(
            Booking(
                program_id=item.program_id,
                prescription=prescription,
                procedure_id=prescription.procedure_id,
                kind=BookingKind.INDIVIDUAL,
                date=day,
                start=slot.start,
                end=slot.end,
                instructor_id=seat.instructor_id,
                slot=slot,
                note=seat.note,
                source=BookingSource.AUTO,
            )
        )
    for booking in existing:
        booking.delete()
    for booking in create:
        booking.save()
    entry = BoardPatient.objects.filter(date=day, program_id=item.program_id).first()
    if entry is None:
        if item.unplaced or item.cancelled:
            BoardPatient.objects.create(
                date=day, program_id=item.program_id, unplaced=item.unplaced,
                cancelled=item.cancelled,
            )  # fmt: skip
    elif not (item.unplaced or item.cancelled or entry.edited):
        entry.delete()
    elif (entry.unplaced, entry.cancelled) != (item.unplaced, item.cancelled):
        entry.unplaced, entry.cancelled = item.unplaced, item.cancelled
        entry.save()


# --- Данные для домена ----------------------------------------------------------------------


def _programs_on(day: date) -> list[Program]:
    """Пациенты с индивидуальным в этот день: день занятий и назначение действует."""
    return list(
        Program.objects.filter(start_date__lt=day, end_date__gt=day)
        .exclude(withdrawn_on(day))
        .filter(
            Q(prescriptions__procedure__kind=ProcedureKind.INDIVIDUAL)
            & (Q(prescriptions__start_date__isnull=True) | Q(prescriptions__start_date__lte=day))
            & (Q(prescriptions__cancel_date__isnull=True) | Q(prescriptions__cancel_date__gt=day))
        )
        .select_related("department")
        .prefetch_related("withdrawals")
        .distinct()
    )


def _patient_order(program: Program) -> tuple:
    return (room_sort_key(program.room), program.full_name, program.pk)


def lessons_on(program: Program, day: date) -> int:
    """Сколько индивидуальных у пациента в этот день (0 — день без занятий)."""
    return len(_individual_prescriptions([program], day)[program.pk])


def _individual_prescriptions(programs: list[Program], day: date) -> dict[int, list[Prescription]]:
    """Индивидуальные занятия пациента на дату — по одному элементу на занятие: назначение
    с «2 р/д» даёт два. Не больше лимита отделения (FR-PRG-3)."""
    result: dict[int, list[Prescription]] = {}
    items = (
        Prescription.objects.filter(program__in=programs, procedure__kind=ProcedureKind.INDIVIDUAL)
        .select_related("procedure")
        .order_by("card_order", "pk")
    )
    by_program: dict[int, list[Prescription]] = defaultdict(list)
    for item in items:
        by_program[item.program_id].append(item)
    for program in programs:
        units: list[Prescription] = []
        if program.is_therapy_day(day):
            for item in by_program[program.pk]:
                if _active(item, program, day):
                    units += [item] * item.per_day
        result[program.pk] = units[: program.department.max_individual_per_day]
    return result


def _active(prescription: Prescription, program: Program, day: date) -> bool:
    start = prescription.start_date or program.start_date
    return start <= day and (prescription.cancel_date is None or day < prescription.cancel_date)


def _evening_note(program: Program, day: date) -> str:
    """Мото-Л / Артромот в назначениях — одно индивидуальное вечером (FR-SCH-8)."""
    for item in program.prescriptions.select_related("procedure").filter(
        procedure__evening_individual=True
    ):
        if _active(item, program, day):
            return item.procedure.card_label
    return ""


def _seats_on(day: date, program_ids: list[int]) -> dict[int, list[Seat]]:
    result: dict[int, list[Seat]] = defaultdict(list)
    for booking in Booking.objects.filter(
        date=day, kind=BookingKind.INDIVIDUAL, program__in=program_ids
    ):
        result[booking.program_id].append(
            Seat(booking.instructor_id, booking.slot_id, booking.note)
        )
    return result


def _busy_on(day: date, program_ids: list[int]) -> dict[int, list[tuple[int, int]]]:
    """Группы, бассейн и закреплённые тренажёры пациента в этот день. Тренажёр, поставленный
    подбором, индивидуальному не мешает — он сдвигается (``fit_trainers``)."""
    result: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for program_id, start, end in (
        Booking.objects.filter(date=day, program__in=program_ids)
        .exclude(kind=BookingKind.INDIVIDUAL)
        .exclude(_movable_trainer())
        .values_list("program_id", "start", "end")
    ):
        result[program_id].append((minutes(start), minutes(end)))
    return result


def _columns(day: date, grid: list[GridSlot]) -> list[Column]:
    teams = build_teams()
    ids = [pk for team in teams for pk in team.member_ids]
    calendars = instructor_calendars(list(Instructor.objects.filter(pk__in=ids)), day, day)
    slot_ids = [slot.id for slot in grid]
    columns = []
    for team in teams:
        members = []
        for pk in team.member_ids:
            calendar = calendars[pk]
            item = calendar.day(day)
            rule = calendar.pattern_on(day)
            members.append(
                Member(
                    pk,
                    item.working,
                    frozenset(item.free_slots(slot_ids)),
                    # Вечером работают только инструкторы 2/2 — у них смена 8:00–20:00.
                    evening=item.working
                    and rule is not None
                    and Pattern(rule.pattern) is Pattern.TWO_TWO,
                )
            )
        columns.append(Column(team.key, tuple(members)))
    return columns
