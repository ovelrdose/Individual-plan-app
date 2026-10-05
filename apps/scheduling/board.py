"""Шахматка на дату (TZ.md §7.6, FR-SCH-14…16): просмотр и правка ячеек.

Колонки — команды инструкторов в смене (пара 2/2 — одна колонка «A/ B», в ячейке работающий
в этот день), строки — слоты сетки. Справа — тренажёры по шагам окна.

Правка — только специалист ФР и администратор (решение 46). Пациент ставится, переносится
и убирается через единый валидатор (FR-SCH-5); правка закрепляется (``pinned``), после неё
расписание программы пересобирается, чтобы предупреждения и конфликты были свежими.
В субботу и воскресенье индивидуальные инструкторам не расписываются (решение 56).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from django.db import transaction
from django.db.models import Q

from apps.accounts.access import departments_for, has_role, has_role_anywhere
from apps.accounts.models import Role, User
from apps.cards.xlsx.board import BoardCell, BoardSheet
from apps.catalog.models import Equipment, InstructorSlot, ProcedureKind
from apps.programs.domain import room_sort_key
from apps.programs.models import Prescription, Program
from apps.staff import domain as staff_domain
from apps.staff.models import BlockKind, Instructor, InstructorBlock
from apps.staff.services import build_teams, instructor_calendars

from . import manual
from .domain import is_weekend
from .models import Booking, BookingKind
from .services import choose_preferred_instructor, replan

# Блоки, которые ставят из ячейки. Ведение группы — постоянный распорядок, его задают на
# экране «Распорядок инструкторов» (FR-STF-5).
CELL_BLOCK_KINDS = (BlockKind.METHOD_WORK, BlockKind.BOS, BlockKind.OTHER)


# Ошибки правки — общие с правкой на странице программы.
BoardError = manual.EditError
ConfirmationRequired = manual.ConfirmationRequired


# --- Права ----------------------------------------------------------------------------------


def can_edit_board(user: User) -> bool:
    """Шахматку правят специалист ФР (в любом отделении) и администратор (решение 46)."""
    return has_role_anywhere(user, Role.REHAB)


def can_edit_patient(user: User, program: Program) -> bool:
    """Пациента ставит и убирает специалист ФР его отделения: чужие — только для чтения."""
    return has_role(user, program.department, Role.REHAB)


def _require_board(user: User) -> None:
    if not can_edit_board(user):
        raise BoardError("Шахматку правит специалист ФР.")


def _require_patient(user: User, program: Program) -> None:
    if not can_edit_patient(user, program):
        raise BoardError("Пациента другого отделения правит специалист ФР этого отделения.")


# --- Просмотр -------------------------------------------------------------------------------


@dataclass
class Seat:
    """Инструктор в ячейке: пациент, обязанность или свободно."""

    instructor: Instructor
    booking: Booking | None = None
    busy: staff_domain.Busy | None = None
    patient: str = ""
    # Ссылка на программу — только для своих отделений (FR-SCH-16).
    program_id: int | None = None

    @property
    def is_free(self) -> bool:
        return self.booking is None and self.busy is None


@dataclass
class Cell:
    slot: InstructorSlot
    seats: list[Seat] = field(default_factory=list)


@dataclass
class Column:
    label: str
    instructors: list[Instructor]
    cells: list[Cell] = field(default_factory=list)


@dataclass
class EquipmentRow:
    """Строка тренажёров: время и по каждому тренажёру — пациенты и «сверх вместимости»."""

    start: time
    in_window: bool
    cells: list[tuple[list[str], bool]]


@dataclass
class Board:
    day: date
    slots: list[InstructorSlot]
    columns: list[Column]
    equipment: list[Equipment]
    equipment_rows: list[EquipmentRow]
    # Выходной: индивидуальные без инструктора, «9:10 — 9п Иванов» (решение 56).
    weekend_individuals: list[tuple[time, str, int | None]]
    can_edit: bool

    @property
    def is_weekend(self) -> bool:
        return is_weekend(self.day)

    @property
    def rows(self) -> list[tuple[InstructorSlot, list[Cell]]]:
        return [
            (slot, [column.cells[index] for column in self.columns])
            for index, slot in enumerate(self.slots)
        ]

    @property
    def previous(self) -> date:
        return self.day - timedelta(days=1)

    @property
    def next(self) -> date:
        return self.day + timedelta(days=1)


def patient_label(program: Program, own: set[int], note: str = "") -> str:
    """«9п Иванов»; пациент чужого отделения — «9п ОМР 1 Иванов» (FR-SCH-14)."""
    parts = [f"{program.room}п"]
    if program.department_id not in own:
        parts.append(program.department.name)
    parts.append(program.surname)
    if note:
        parts.append(note)
    return " ".join(parts)


def board(user: User | None, day: date) -> Board:
    """Шахматка на дату. Видят все сотрудники; правят — специалист ФР и администратор.

    ``user = None`` — публичная шахматка (FR-ACC-5): все пациенты «палата, отделение,
    фамилия», без ссылок и правки."""
    own = set(departments_for(user).values_list("pk", flat=True)) if user else set()
    slots = list(InstructorSlot.objects.order_by("start"))
    teams = build_teams()
    instructors = {item.pk: item for item in Instructor.objects.filter(pk__in=_ids(teams))}
    calendars = instructor_calendars(list(instructors.values()), day, day)
    bookings = Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL).select_related(
        "program__department"
    )
    by_seat: dict[tuple[int, int], Booking] = {}
    weekend: list[tuple[time, str, int | None]] = []
    for item in bookings:
        label = patient_label(item.program, own, item.note)
        link = item.program_id if item.program.department_id in own else None
        if item.instructor_id is None:
            weekend.append((item.start, label, link))
        else:
            by_seat[(item.instructor_id, item.slot_id)] = item

    columns: list[Column] = []
    for team in teams:
        members = [instructors[pk] for pk in team.member_ids]
        days = {pk: calendars[pk].day(day) for pk in team.member_ids}
        booked = {pk for pk, _slot in by_seat if pk in days}
        shown = [m for m in members if days[m.pk].working or m.pk in booked]
        if not shown:
            continue
        column = Column(team.label, shown)
        for slot in slots:
            cell = Cell(slot)
            for member in shown:
                booking = by_seat.get((member.pk, slot.pk))
                busy = days[member.pk].busy.get(slot.pk)
                if booking is None and busy is None and not days[member.pk].working:
                    continue
                seat = Seat(member, booking=booking, busy=busy if booking is None else None)
                if booking is not None:
                    seat.patient = patient_label(booking.program, own, booking.note)
                    if booking.program.department_id in own:
                        seat.program_id = booking.program_id
                cell.seats.append(seat)
            column.cells.append(cell)
        columns.append(column)

    equipment, equipment_rows = _equipment_table(day, own)
    return Board(
        day=day,
        slots=slots,
        columns=columns,
        equipment=equipment,
        equipment_rows=equipment_rows,
        weekend_individuals=sorted(weekend, key=lambda item: (item[0], item[1])),
        can_edit=can_edit_board(user) if user else False,
    )


def _ids(teams: list[staff_domain.Team]) -> list[int]:
    return [pk for team in teams for pk in team.member_ids]


def _equipment_table(day: date, own: set[int]) -> tuple[list[Equipment], list[EquipmentRow]]:
    """Тренажёры: строки — шаги окна, ниже — время ручных записей вне окна (FR-SCH-14)."""
    equipment = list(Equipment.objects.filter(is_active=True).order_by("name"))
    window = {start for item in equipment for start in item.start_times()}
    cells: dict[tuple[int, time], list[str]] = defaultdict(list)
    bookings = Booking.objects.filter(
        date=day, kind=BookingKind.EQUIPMENT, equipment__in=equipment
    ).select_related("program__department")
    starts = set(window)
    for item in bookings.order_by("start", "program__room"):
        cells[(item.equipment_id, item.start)].append(patient_label(item.program, own, item.note))
        starts.add(item.start)
    rows = [
        EquipmentRow(
            start,
            start in window,
            [
                (cells[(item.pk, start)], len(cells[(item.pk, start)]) > item.capacity)
                for item in equipment
            ],
        )
        for start in sorted(starts, key=lambda s: (s not in window, s))
    ]
    return equipment, rows


def sheet(result: Board) -> BoardSheet:
    """Шахматка для выгрузки в .xlsx (FR-CRD-7): то же, что на экране, без ссылок."""
    cells = []
    for _slot, row in result.rows:
        line = []
        for cell in row:
            texts = [
                (f"{seat.instructor.short_name}: " if len(cell.seats) > 1 else "")
                + (seat.patient or (seat.busy.text if seat.busy else ""))
                for seat in cell.seats
            ]
            busy = bool(cell.seats) and all(seat.busy is not None for seat in cell.seats)
            line.append(BoardCell("\n".join(t for t in texts if t.strip(": ")), busy))
        cells.append(line)
    return BoardSheet(
        day=result.day,
        slots=[(slot.start, slot.end) for slot in result.slots],
        columns=[column.label for column in result.columns],
        cells=cells,
        equipment=[item.name for item in result.equipment],
        equipment_times=[row.start for row in result.equipment_rows],
        equipment_cells=[row.cells for row in result.equipment_rows],
    )


# --- Выбор для формы ячейки -----------------------------------------------------------------


def programs_for_cell(user: User, day: date) -> list[Program]:
    """Кого можно поставить в ячейку: программы отделений, где пользователь — специалист ФР,
    с индивидуальным назначением, действующим в эту дату (FR-SCH-15)."""
    departments = [d for d in departments_for(user) if has_role(user, d, Role.REHAB)]
    programs = (
        Program.objects.filter(department__in=departments, start_date__lte=day, end_date__gte=day)
        .filter(
            Q(prescriptions__procedure__kind=ProcedureKind.INDIVIDUAL)
            & (Q(prescriptions__start_date__isnull=True) | Q(prescriptions__start_date__lte=day))
            & (Q(prescriptions__cancel_date__isnull=True) | Q(prescriptions__cancel_date__gt=day))
        )
        .select_related("department")
        .distinct()
    )
    return sorted(programs, key=lambda p: (p.department.name, room_sort_key(p.room), p.full_name))


def free_seats(
    day: date, exclude: Booking | None = None
) -> list[tuple[Instructor, InstructorSlot]]:
    """Куда перенести пациента в этот день: инструктор работает, слот без обязанности и
    без пациента; вечерние слоты тоже — их ставят только вручную (FR-CAT-5)."""
    if is_weekend(day):
        return []
    teams = build_teams()
    instructors = list(Instructor.objects.filter(pk__in=_ids(teams)))
    calendars = instructor_calendars(instructors, day, day)
    taken = set(
        Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL, instructor__isnull=False)
        .exclude(pk=exclude.pk if exclude else None)
        .values_list("instructor_id", "slot_id")
    )
    order = {pk: index for index, pk in enumerate(_ids(teams))}
    result = [
        (item, slot)
        for slot in InstructorSlot.objects.order_by("start")
        for item in sorted(instructors, key=lambda i: order[i.pk])
        if calendars[item.pk].day(day).is_free(slot.pk) and (item.pk, slot.pk) not in taken
    ]
    return result


# --- Правка: пациент ------------------------------------------------------------------------


@transaction.atomic
def place_patient(
    user: User,
    program: Program,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    *,
    reason: str = "",
) -> Booking:
    """Поставить пациента в ячейку (FR-SCH-15). Если у него в этот день есть занятие,
    поставленное подбором, оно переносится сюда — так специалист чаще всего и работает;
    иначе добавляется новое. Правка закрепляется (``pinned``)."""
    _require_board(user)
    program = manual.lock_program(program)
    _require_patient(user, program)
    prescription = _individual_prescription(program, day)
    moved = (
        program.bookings.filter(
            date=day, kind=BookingKind.INDIVIDUAL, pinned=False, prescription=prescription
        )
        .order_by("start")
        .first()
    )
    return _put(user, program, prescription, instructor, slot, day, moved, reason)


@transaction.atomic
def move_patient(
    user: User,
    booking: Booking,
    version: int,
    instructor: Instructor,
    slot: InstructorSlot,
    *,
    reason: str = "",
) -> Booking:
    """Перенести пациента в другую ячейку той же даты."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    booking = manual.fresh(program, booking, version)
    return _put(
        user, program, booking.prescription, instructor, slot, booking.date, booking, reason
    )


@transaction.atomic
def remove_patient(user: User, booking: Booking, version: int) -> None:
    """Убрать пациента из ячейки: занятия в эту дату не будет, подбор его не вернёт
    (``RemovedSession``). Вернуть — поставить пациента в любую свободную ячейку."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    booking = manual.fresh(program, booking, version)
    manual.add_removal(user, booking.prescription, booking.date)
    booking._history_user = user
    booking.delete()
    replan(program)


@transaction.atomic
def prefer_instructor(user: User, booking: Booking) -> None:
    """«Закрепить за этим инструктором» из ячейки (FR-SCH-15): инструктор становится
    инструктором по желанию пациента, индивидуальные программы пересобираются у него."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    if booking.instructor is None:
        raise BoardError("У занятия нет инструктора.")
    choose_preferred_instructor(user, program, booking.instructor)


def _individual_prescription(program: Program, day: date) -> Prescription:
    for item in program.prescriptions.select_related("procedure").order_by("pk"):
        if item.procedure is None or item.procedure.kind != ProcedureKind.INDIVIDUAL:
            continue
        if manual.is_active_on(item, program, day):
            return item
    raise BoardError(f"У пациента нет индивидуального занятия на {day:%d.%m}.")


def _put(
    user: User,
    program: Program,
    prescription: Prescription,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    moved: Booking | None,
    reason: str,
) -> Booking:
    if is_weekend(day):
        raise BoardError(
            "В субботу и воскресенье индивидуальные ведут дежурные 2/2 без шахматки (решение 56)."
        )
    if not instructor.is_active:
        raise BoardError("Инструктор не действует.")
    manual.lock_resources(program)
    violations = manual.check(
        program, prescription, day, slot.start, slot.end,
        moved=moved, instructor=instructor, slot=slot,
    )  # fmt: skip
    hard, soft = manual.split(violations)
    if hard:
        raise BoardError(" ".join(v.message for v in hard), hard)
    if soft and not reason.strip():
        raise ConfirmationRequired(" ".join(v.message for v in soft), soft)
    booking = moved or Booking(
        program=program,
        prescription=prescription,
        procedure=prescription.procedure,
        kind=BookingKind.INDIVIDUAL,
        date=day,
    )
    if moved is None:
        manual.undo_removal(user, prescription, day)
    booking.instructor = instructor
    booking.slot = slot
    booking.start, booking.end = slot.start, slot.end
    manual.save_manual(user, booking, reason)
    replan(program)
    return booking


# --- Правка: блоки --------------------------------------------------------------------------


@dataclass
class BlockResult:
    created: list[date]
    skipped: list[date]


@transaction.atomic
def add_block(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    label: str = "",
    until: date | None = None,
) -> BlockResult:
    """Блок в ячейке (FR-SCH-15): на дату или «ежедневно по дату». Даты, где в ячейке уже
    стоит пациент, пропускаются — их показывают списком: пациента сначала переносят."""
    _require_board(user)
    if kind not in CELL_BLOCK_KINDS:
        raise BoardError("Выберите вид блока.")
    if kind == BlockKind.OTHER and not label.strip():
        raise BoardError("Укажите, чем занят инструктор.")
    until = until or day
    if until < day:
        raise BoardError("«По дату» раньше даты ячейки.")
    if (until - day).days > 92:
        raise BoardError("Блок ставится не больше чем на три месяца вперёд.")
    instructor = Instructor.objects.select_for_update().get(pk=instructor.pk)
    days = [day + timedelta(days=i) for i in range((until - day).days + 1)]
    booked = set(
        Booking.objects.filter(
            instructor=instructor, slot=slot, date__in=days, kind=BookingKind.INDIVIDUAL
        ).values_list("date", flat=True)
    )
    result = BlockResult([], [])
    for current in days:
        if current in booked:
            result.skipped.append(current)
            continue
        block = InstructorBlock.objects.filter(
            instructor=instructor, slot=slot, date=current
        ).first() or InstructorBlock(instructor=instructor, slot=slot, date=current)
        block.kind = kind
        block.label = label.strip() if kind == BlockKind.OTHER else ""
        block.group_session = None
        block.full_clean()
        block._history_user = user
        block.save()
        result.created.append(current)
    return result


@transaction.atomic
def clear_block(user: User, instructor: Instructor, slot: InstructorSlot, day: date) -> None:
    """Убрать блок из ячейки: разовый — удаляется, постоянный распорядок — снимается на эту
    дату (блок «Снять распорядок», FR-STF-5)."""
    _require_board(user)
    instructor = Instructor.objects.select_for_update().get(pk=instructor.pk)
    busy = instructor_calendars([instructor], day, day)[instructor.pk].day(day).busy.get(slot.pk)
    if busy is None:
        raise BoardError("В ячейке нет блока.")
    block = InstructorBlock.objects.filter(instructor=instructor, slot=slot, date=day).first()
    if busy.one_off:
        if block is None:
            # Разовый блок группы стоит в соседнем слоте и захватывает этот.
            raise BoardError("Блок поставлен в соседний слот — уберите его там.")
        block._history_user = user
        block.delete()
        return
    block = block or InstructorBlock(instructor=instructor, slot=slot, date=day)
    block.kind = BlockKind.CLEAR
    block.label = ""
    block.group_session = None
    block.full_clean()
    block._history_user = user
    block.save()
