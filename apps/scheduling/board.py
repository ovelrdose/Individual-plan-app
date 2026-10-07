"""Шахматка на дату (TZ.md §7.3–7.4, FR-SCH-10…15): просмотр и правка ячеек.

Колонки — команды инструкторов в смене (пара 2/2 — одна колонка «A/ B», в ячейке работающий
в этот день), строки — слоты сетки. В ячейке может быть несколько пациентов. Под сеткой —
блоки «Не распределены» и «Отменены». Справа — тренажёры по шагам окна.

Правка — только специалист ФР и администратор и только сегодняшней и завтрашней шахматки.
Пациент ставится, переносится и убирается через единый валидатор; правка сегодняшней
шахматки переходит на завтрашнюю (``board_days.propagate``). Шахматка — только по будням.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.accounts.access import departments_for, has_role, has_role_anywhere
from apps.accounts.models import Role, User
from apps.cards.xlsx.board import BoardCell, BoardSheet
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, ProcedureKind
from apps.programs.domain import fold, room_sort_key
from apps.programs.models import Prescription, Program, withdrawn_on
from apps.staff import domain as staff_domain
from apps.staff.models import BlockKind, Instructor, InstructorBlock, InstructorDuty
from apps.staff.services import build_teams, instructor_calendars

from . import board_days, manual, staff_changes
from .domain import is_weekend
from .models import BoardDay, BoardPatient, Booking, BookingKind

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")

# Что ставят из ячейки — разовым блоком или постоянным распорядком (FR-STF-5).
CELL_BLOCK_KINDS = (BlockKind.METHOD_WORK, BlockKind.BOS, BlockKind.GROUP_LEAD, BlockKind.OTHER)


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
class Patient:
    """Пациент в ячейке или в блоке: подпись и ссылка (только для своих отделений)."""

    label: str
    program_id: int | None = None
    booking: Booking | None = None
    # Другие занятия пациента в этот день — JSON для подсветки при переносе (FR-SCH-10a).
    busy: str = ""


@dataclass
class Seat:
    """Инструктор в ячейке: пациенты, обязанность или свободно."""

    instructor: Instructor
    patients: list[Patient] = field(default_factory=list)
    busy: staff_domain.Busy | None = None

    @property
    def is_free(self) -> bool:
        return not self.patients and self.busy is None


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
    cells: list[tuple[Equipment, list[Patient], bool]]


@dataclass
class Board:
    day: date
    slots: list[InstructorSlot]
    columns: list[Column]
    equipment: list[Equipment]
    equipment_rows: list[EquipmentRow]
    # Шахматка на этот день составлена (FR-SCH-5); правка — сегодня и завтра.
    exists: bool
    can_edit: bool
    unplaced: list[tuple[Patient, int]] = field(default_factory=list)
    cancelled: list[tuple[Patient, int]] = field(default_factory=list)

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
        day = self.day - timedelta(days=1)
        while is_weekend(day):
            day -= timedelta(days=1)
        return day

    @property
    def next(self) -> date:
        return board_days.next_weekday(self.day)

    @property
    def following(self) -> date:
        """Ближайшая шахматка на день вперёд — от сегодняшней даты, а не от показанной."""
        return board_days.next_weekday(board_days.today())

    @property
    def has_next(self) -> bool:
        """Вперёд листать можно только до завтрашней шахматки (FR-SCH-14)."""
        return self.next <= self.following

    @property
    def following_label(self) -> str:
        """«Завтра», а в пятницу — «Понедельник»: по выходным шахматки нет."""
        if self.following == board_days.today() + timedelta(days=1):
            return "Завтра"
        return WEEKDAYS[self.following.weekday()]

    @property
    def hold_blocks(self) -> list[tuple[str, list[tuple[Patient, int]], str]]:
        """Блоки под сеткой (FR-SCH-11): название, пациенты с числом занятий, подсказка."""
        return [
            (
                "Не распределены",
                self.unplaced,
                "Не хватило окон или инструктор не вышел — поставьте в свободную ячейку.",
            ),
            (
                "Отменены",
                self.cancelled,
                "Убраны из сетки вручную; остаются здесь, пока пациент лечится.",
            ),
        ]


def patient_label(program: Program, own: set[int], note: str = "") -> str:
    """«9п Иванов»; пациент чужого отделения — «9п ОМР 1 Иванов» (FR-SCH-14)."""
    parts = [program.room_label]
    if program.department_id not in own:
        parts.append(program.department.name)
    parts.append(program.surname)
    if note:
        parts.append(note)
    return " ".join(parts)


def board(user: User | None, day: date) -> Board:
    """Шахматка на дату. Видят все сотрудники; правят — специалист ФР и администратор.

    ``user = None`` — публичная шахматка (FR-SCH-15): все пациенты «палата, отделение,
    фамилия», без ссылок, блоков и правки."""
    own = set(departments_for(user).values_list("pk", flat=True)) if user else set()
    slots = list(InstructorSlot.objects.order_by("start"))
    teams = build_teams()
    instructors = {item.pk: item for item in Instructor.objects.filter(pk__in=_ids(teams))}
    calendars = instructor_calendars(list(instructors.values()), day, day)
    bookings = (
        Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL)
        .select_related("program__department")
        .order_by("program__room", "pk")
    )
    by_seat: dict[tuple[int, int], list[Patient]] = defaultdict(list)
    for item in bookings:
        by_seat[(item.instructor_id, item.slot_id)].append(_patient(item.program, own, item))

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
                patients = by_seat.get((member.pk, slot.pk), [])
                busy = days[member.pk].busy.get(slot.pk)
                if not patients and busy is None and not days[member.pk].working:
                    continue
                cell.seats.append(
                    Seat(member, patients=patients, busy=busy if not patients else None)
                )
            column.cells.append(cell)
        columns.append(column)

    equipment, equipment_rows = _equipment_table(day, own)
    exists = BoardDay.objects.filter(date=day).exists()
    result = Board(
        day=day,
        slots=slots,
        columns=columns,
        equipment=equipment,
        equipment_rows=equipment_rows,
        exists=exists,
        can_edit=bool(user) and can_edit_board(user) and board_days.editable(day),
    )
    if user is not None:
        holds = BoardPatient.objects.filter(date=day).select_related("program__department")
        for item in sorted(holds, key=lambda h: (room_sort_key(h.program.room), h.program.pk)):
            patient = _patient(item.program, own)
            if item.unplaced:
                result.unplaced.append((patient, item.unplaced))
            if item.cancelled:
                result.cancelled.append((patient, item.cancelled))
        _fill_busy(result)
    return result


def _all_patients(result: Board) -> list[Patient]:
    seated = [
        p
        for column in result.columns
        for cell in column.cells
        for s in cell.seats
        for p in s.patients
    ]
    on_equipment = [p for row in result.equipment_rows for _i, ps, _o in row.cells for p in ps]
    held = [p for p, _units in result.unplaced + result.cancelled]
    return seated + on_equipment + held


def _fill_busy(result: Board) -> None:
    """Занятия своих пациентов — чтобы при переносе подсветить время, где пациент занят."""
    patients = [p for p in _all_patients(result) if p.program_id is not None]
    taken = occupied({p.program_id for p in patients}, result.day)
    for patient in patients:
        skip = patient.booking.pk if patient.booking is not None else None
        patient.busy = busy_json(taken.get(patient.program_id, []), skip)


@dataclass(frozen=True)
class Occupied:
    """Занятие пациента в день: минуты начала и конца и что это."""

    booking_id: int
    start: int
    end: int
    label: str


def occupied(program_ids: set[int], day: date) -> dict[int, list[Occupied]]:
    """Все занятия пациентов в дату: группы, бассейн, тренажёры, индивидуальные."""
    result: dict[int, list[Occupied]] = defaultdict(list)
    bookings = (
        Booking.objects.filter(date=day, program_id__in=program_ids)
        .select_related("procedure", "instructor", "equipment")
        .order_by("start")
    )
    for item in bookings:
        if item.kind == BookingKind.INDIVIDUAL and item.instructor is not None:
            label = f"инд. {item.instructor.short_name}"
        elif item.equipment is not None:
            label = item.equipment.name
        else:
            label = item.procedure.card_label or item.procedure.name
        result[item.program_id].append(
            Occupied(item.pk, manual.minutes(item.start), manual.minutes(item.end), label)
        )
    return result


def busy_json(items: list[Occupied], skip: int | None = None) -> str:
    """[[начало, конец, «9:10 Эрго общая»], …] без переносимого занятия."""
    return json.dumps(
        [
            [item.start, item.end, f"{item.start // 60}:{item.start % 60:02d} {item.label}"]
            for item in items
            if item.booking_id != skip
        ],
        ensure_ascii=False,
    )


def _patient(program: Program, own: set[int], booking: Booking | None = None) -> Patient:
    return Patient(
        patient_label(program, own, booking.note if booking else ""),
        program.pk if program.department_id in own else None,
        booking,
    )


def _ids(teams: list[staff_domain.Team]) -> list[int]:
    return [pk for team in teams for pk in team.member_ids]


def _equipment_table(day: date, own: set[int]) -> tuple[list[Equipment], list[EquipmentRow]]:
    """Тренажёры: строки — шаги окна, ниже — время ручных записей вне окна (FR-SCH-14)."""
    equipment = list(Equipment.objects.filter(is_active=True).order_by("name"))
    window = {start for item in equipment for start in item.start_times()}
    cells: dict[tuple[int, time], list[Patient]] = defaultdict(list)
    bookings = Booking.objects.filter(
        date=day, kind=BookingKind.EQUIPMENT, equipment__in=equipment
    ).select_related("program__department")
    starts = set(window)
    for item in bookings.order_by("start", "program__room"):
        cells[(item.equipment_id, item.start)].append(_patient(item.program, own, item))
        starts.add(item.start)
    rows = [
        EquipmentRow(
            start,
            start in window,
            [
                (item, cells[(item.pk, start)], len(cells[(item.pk, start)]) > item.capacity)
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
                + (
                    ", ".join(p.label for p in seat.patients)
                    or (seat.busy.text if seat.busy else "")
                )
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
        equipment_cells=[
            [([p.label for p in patients], over) for _item, patients, over in row.cells]
            for row in result.equipment_rows
        ],
    )


# --- Выбор для формы ячейки -----------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """Пациент в списке ячейки (FR-SCH-10): кого можно поставить и где он сейчас."""

    program: Program
    group: str  # UNPLACED, CANCELLED или OTHER — порядок в списке
    where: str = ""  # «9:50 Соколов» — уже стоит в сетке

    @property
    def group_label(self) -> str:
        return CANDIDATE_GROUPS[self.group]

    @property
    def search(self) -> str:
        """Для поиска по палате и фамилии без учёта регистра и «ё»."""
        return fold(f"{self.program.room_label} {self.program.surname} {self.program.room}")


UNPLACED, CANCELLED, OTHER = "unplaced", "cancelled", "other"
CANDIDATE_GROUPS = {UNPLACED: "Не распределены", CANCELLED: "Отменены", OTHER: "На лечении"}


def programs_for_cell(user: User, day: date) -> list[Candidate]:
    """Кого можно поставить в ячейку: пациенты на лечении с индивидуальным в эту дату из
    отделений, где пользователь — специалист ФР. Сначала «Не распределены», затем
    «Отменены», затем остальные, внутри — по палатам (FR-SCH-10)."""
    departments = [d for d in departments_for(user) if has_role(user, d, Role.REHAB)]
    programs = (
        Program.objects.filter(department__in=departments, start_date__lt=day, end_date__gt=day)
        .exclude(withdrawn_on(day))
        .filter(
            Q(prescriptions__procedure__kind=ProcedureKind.INDIVIDUAL)
            & (Q(prescriptions__start_date__isnull=True) | Q(prescriptions__start_date__lte=day))
            & (Q(prescriptions__cancel_date__isnull=True) | Q(prescriptions__cancel_date__gt=day))
        )
        .select_related("department")
        .distinct()
    )
    holds = {item.program_id: item for item in BoardPatient.objects.filter(date=day)}
    placed: dict[int, list[str]] = defaultdict(list)
    for item in (
        Booking.objects.filter(date=day, kind=BookingKind.INDIVIDUAL, program__in=programs)
        .select_related("instructor")
        .order_by("start")
    ):
        placed[item.program_id].append(f"{item.start:%-H:%M} {item.instructor.short_name}")
    result = []
    for program in programs:
        hold = holds.get(program.pk)
        if hold is not None and hold.unplaced:
            group = UNPLACED
        elif hold is not None and hold.cancelled:
            group = CANCELLED
        else:
            group = OTHER
        result.append(Candidate(program, group, ", ".join(placed[program.pk])))
    order = list(CANDIDATE_GROUPS)
    return sorted(
        result,
        key=lambda c: (order.index(c.group), room_sort_key(c.program.room), c.program.full_name),
    )


def free_seats(
    day: date, exclude: Booking | None = None
) -> list[tuple[Instructor, InstructorSlot]]:
    """Куда перенести пациента в этот день: инструктор работает и слот без постоянной
    занятости. Пациенты в ячейке не мешают — их может быть несколько (FR-SCH-10); вечерние
    слоты вручную доступны любому пациенту."""
    if is_weekend(day):
        return []
    teams = build_teams()
    instructors = list(Instructor.objects.filter(pk__in=_ids(teams)))
    calendars = instructor_calendars(instructors, day, day)
    order = {pk: index for index, pk in enumerate(_ids(teams))}
    return [
        (item, slot)
        for slot in InstructorSlot.objects.order_by("start")
        for item in sorted(instructors, key=lambda i: order[i.pk])
        if calendars[item.pk].day(day).is_free(slot.pk)
    ]


# --- Правка: пациент ------------------------------------------------------------------------


@transaction.atomic
def place_patient(
    user: User, program: Program, instructor: Instructor, slot: InstructorSlot, day: date
) -> Booking:
    """Поставить пациента в ячейку (FR-SCH-10) — из списка, «Не распределены» или «Отменены».
    Занятие берётся сначала из «Не распределены», затем из «Отменены». Если свободных
    занятий нет, а в сетке пациент стоит один раз — он переносится сюда."""
    _require_board(user)
    program = manual.lock_program(program)
    _require_patient(user, program)
    _require_editable(day)
    board_days.lock()
    entry = BoardPatient.objects.filter(date=day, program=program).first()
    held = (entry.unplaced + entry.cancelled) if entry is not None else 0
    current = list(program.bookings.filter(date=day, kind=BookingKind.INDIVIDUAL))
    if not held and current and len(current) >= board_days.lessons_on(program, day):
        if len(current) > 1:
            raise BoardError(
                f"У пациента уже {len(current)} индивидуальных в этот день — перенесите нужное "
                "из его ячейки."
            )
        booking = current[0]
        _put(user, program, booking, instructor, slot)
        board_days.mark_edited(program, day)
        board_days.propagate(program, day)
        return booking
    prescription = _individual_prescription(program, day)
    booking = Booking(
        program=program,
        prescription=prescription,
        procedure=prescription.procedure,
        kind=BookingKind.INDIVIDUAL,
        date=day,
    )
    _put(user, program, booking, instructor, slot)
    unplaced = -1 if entry is not None and entry.unplaced else 0
    cancelled = -1 if entry is not None and not entry.unplaced and entry.cancelled else 0
    board_days.mark_edited(program, day, unplaced=unplaced, cancelled=cancelled)
    board_days.propagate(program, day)
    return booking


@transaction.atomic
def move_patient(
    user: User, booking: Booking, version: int, instructor: Instructor, slot: InstructorSlot
) -> Booking:
    """Перенести пациента в другую ячейку той же даты (в том числе перетаскиванием)."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    booking = manual.fresh(program, booking, version)
    _require_editable(booking.date)
    _put(user, program, booking, instructor, slot)
    board_days.mark_edited(program, booking.date)
    board_days.propagate(program, booking.date)
    return booking


@transaction.atomic
def set_note(user: User, booking: Booking, version: int, note: str) -> Booking:
    """Пометка в ячейке: «Мото-Л», «art», «(шар)» (FR-SCH-10). Переходит на завтра вместе с
    пациентом."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    booking = manual.fresh(program, booking, version)
    _require_editable(booking.date)
    board_days.lock()
    booking.note = " ".join(note.split())[:100]
    manual.save_manual(user, booking)
    board_days.mark_edited(program, booking.date)
    board_days.propagate(program, booking.date)
    return booking


@transaction.atomic
def remove_patient(user: User, booking: Booking, version: int) -> None:
    """Убрать пациента из сетки: он уходит в «Отменены» и остаётся там на следующие дни,
    пока лечится (FR-SCH-11). Вернуть — поставить в любую ячейку."""
    _require_board(user)
    program = manual.lock_program(booking.program)
    _require_patient(user, program)
    booking = manual.fresh(program, booking, version)
    _require_editable(booking.date)
    board_days.lock()
    booking._history_user = user
    booking.delete()
    board_days.mark_edited(program, booking.date, cancelled=1)
    board_days.propagate(program, booking.date)


# --- Правка: тренажёры ----------------------------------------------------------------------


def move_equipment(
    user: User, booking: Booking, version: int, start: time, reason: str = ""
) -> manual.EditResult:
    """Перенести пациента на другое время того же тренажёра перетаскиванием (FR-SCH-10a).

    Только эта дата: следующие дни правятся на странице программы. В одно время может стоять
    несколько пациентов в пределах вместимости; сверх неё — с причиной, она видна пометкой."""
    _require_board(user)
    if booking.kind != BookingKind.EQUIPMENT:
        raise BoardError("Это не запись на тренажёр.")
    _require_patient(user, booking.program)
    # Без ensure_boards: она берёт блокировку шахматок раньше программы (порядок — CLAUDE.md).
    if not board_days.editable(booking.date):
        raise BoardError("Править можно только сегодняшнюю и завтрашнюю шахматку (по будням).")
    return manual.edit_booking(user, booking, version, manual.Target(start=start), reason=reason)


def _require_editable(day: date) -> None:
    board_days.ensure_boards()
    if not board_days.editable(day):
        raise BoardError("Править можно только сегодняшнюю и завтрашнюю шахматку (по будням).")


def _individual_prescription(program: Program, day: date) -> Prescription:
    for item in program.prescriptions.select_related("procedure").order_by("card_order", "pk"):
        if item.procedure is None or item.procedure.kind != ProcedureKind.INDIVIDUAL:
            continue
        if manual.is_active_on(item, program, day):
            return item
    raise BoardError(f"У пациента нет индивидуального занятия на {day:%d.%m}.")


def _put(
    user: User, program: Program, booking: Booking, instructor: Instructor, slot: InstructorSlot
) -> None:
    """Проверить и сохранить ячейку. В шахматке любое нарушение не даёт сохранить (FR-SCH-10):
    пациент занят, слот закрыт занятостью, инструктор не работает, два занятия подряд,
    занятий больше, чем назначено."""
    if not instructor.is_active:
        raise BoardError("Инструктор не действует.")
    board_days.lock()
    # Тренажёр, поставленный подбором, уступает место: сдвигается в своём окне. Не вышло —
    # проверка ниже назовёт, чем занят пациент (транзакция откатит сдвиги).
    others = program.bookings.filter(date=booking.date, kind=BookingKind.INDIVIDUAL)
    if booking.pk:
        others = others.exclude(pk=booking.pk)
    seats = [(board_days.minutes(b.start), board_days.minutes(b.end)) for b in others]
    seats.append((board_days.minutes(slot.start), board_days.minutes(slot.end)))
    board_days.fit_trainers(program.pk, booking.date, seats)
    violations = manual.check(
        program, booking.prescription, booking.date, slot.start, slot.end,
        moved=booking if booking.pk else None, instructor=instructor, slot=slot,
    )  # fmt: skip
    if violations:
        raise BoardError(" ".join(v.message for v in violations), violations)
    booking.instructor = instructor
    booking.slot = slot
    booking.start, booking.end = slot.start, slot.end
    manual.save_manual(user, booking)


# --- Правка: блоки --------------------------------------------------------------------------


@dataclass
class BlockResult:
    created: list[date]
    skipped: list[date]


def group_sessions_for(slot: InstructorSlot) -> list[GroupSession]:
    """Занятия групп ЛФК, которые идут в этом слоте: их инструктор может вести (FR-STF-5)."""
    sessions = (
        GroupSession.objects.filter(
            procedure__kind=ProcedureKind.LFK_GROUP, procedure__is_active=True, is_active=True
        )
        .select_related("procedure")
        .order_by("start_time", "procedure__name")
    )
    return [g for g in sessions if g.start_time < slot.end and slot.start < g.end_time]


def _people(instructor: Instructor, with_partner: bool) -> list[Instructor]:
    """Инструктор и, если просили, его напарник по 2/2 — у пары одна колонка шахматки."""
    people = [instructor]
    if with_partner and instructor.partner_id and instructor.partner.is_active:
        people.append(instructor.partner)
    return people


def _check_duty_fields(kind: str, label: str, session: GroupSession | None) -> None:
    if kind not in CELL_BLOCK_KINDS:
        raise BoardError("Выберите вид.")
    if kind == BlockKind.OTHER and not label.strip():
        raise BoardError("Укажите, чем занят инструктор.")
    if kind == BlockKind.GROUP_LEAD and session is None:
        raise BoardError("Выберите группу, которую ведёт инструктор.")


def _covered(slot: InstructorSlot, session: GroupSession | None) -> list[InstructorSlot]:
    """Слоты, которые займёт обязанность: свой, а ведение группы — все пересекающиеся."""
    if session is None:
        return [slot]
    return [
        item
        for item in InstructorSlot.objects.all()
        if item.pk == slot.pk or (item.start < session.end_time and session.start_time < item.end)
    ]


@transaction.atomic
def add_block(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    label: str = "",
    until: date | None = None,
    *,
    group_session: GroupSession | None = None,
    with_partner: bool = False,
) -> BlockResult:
    """Разовый блок в ячейке (FR-SCH-15): на дату или «ежедневно по дату», и поверх
    распорядка («сегодня вместо Метод. работы — БОС»). Даты, где в ячейке уже стоит пациент,
    пропускаются — их показывают списком: пациента сначала переносят."""
    _require_board(user)
    _check_duty_fields(kind, label, group_session)
    until = until or day
    if until < day:
        raise BoardError("«По дату» раньше даты ячейки.")
    if (until - day).days > 92:
        raise BoardError("Блок ставится не больше чем на три месяца вперёд.")
    people = _people(instructor, with_partner)
    list(
        Instructor.objects.select_for_update().filter(pk__in=[p.pk for p in people]).order_by("pk")
    )
    days = [day + timedelta(days=i) for i in range((until - day).days + 1)]
    covered = _covered(slot, group_session)
    result = BlockResult([], [])
    for person in people:
        booked = set(
            Booking.objects.filter(
                instructor=person, slot__in=covered, date__in=days, kind=BookingKind.INDIVIDUAL
            ).values_list("date", flat=True)
        )
        for current in days:
            if current in booked:
                result.skipped.append(current)
                continue
            block = InstructorBlock.objects.filter(
                instructor=person, slot=slot, date=current
            ).first() or InstructorBlock(instructor=person, slot=slot, date=current)
            block.kind = kind
            block.label = label.strip() if kind == BlockKind.OTHER else ""
            block.group_session = group_session if kind == BlockKind.GROUP_LEAD else None
            _full_clean(block)
            block._history_user = user
            block.save()
            result.created.append(current)
    result.skipped = sorted(set(result.skipped))
    result.created = sorted(set(result.created))
    return result


def _full_clean(item: InstructorBlock | InstructorDuty) -> None:
    try:
        item.full_clean()
    except ValidationError as error:
        raise BoardError(" ".join(error.messages)) from None


# --- Постоянный распорядок из ячейки (FR-STF-5) --------------------------------------------


def duty_at(instructor: Instructor, slot: InstructorSlot, day: date) -> InstructorDuty | None:
    """Обязанность распорядка, которая в эту дату занимает слот (в том числе ведение группы,
    захватывающее соседний слот)."""
    duties = instructor.duties.filter(valid_from__lte=day).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=day)
    )
    for duty in duties.select_related("slot", "group_session__procedure"):
        if slot in _covered(duty.slot, duty.group_session):
            return duty
    return None


@dataclass
class DutyResult:
    duties: list[InstructorDuty]


def add_duty(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    label: str = "",
    until: date | None = None,
    *,
    group_session: GroupSession | None = None,
    with_partner: bool = False,
) -> DutyResult:
    """Постоянный распорядок из ячейки: с даты ячейки, бессрочно или по дату. Если в слоте
    уже есть обязанность — она заканчивается накануне («изменить с этой даты»). Пациенты,
    которые стоят в этом слоте в даты распорядка, уходят в «Не распределены» (FR-STF-6)."""
    duties = _save_duties(
        user, instructor, slot, day, kind, label, until, group_session, with_partner
    )
    for duty in duties:
        staff_changes.after_duty(user, duty)
    return DutyResult(duties)


@transaction.atomic
def _save_duties(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    label: str,
    until: date | None,
    session: GroupSession | None,
    with_partner: bool,
) -> list[InstructorDuty]:
    _require_board(user)
    _check_duty_fields(kind, label, session)
    if until is not None and until < day:
        raise BoardError("«По дату» раньше даты ячейки.")
    people = _people(instructor, with_partner)
    list(
        Instructor.objects.select_for_update().filter(pk__in=[p.pk for p in people]).order_by("pk")
    )
    result = []
    for person in people:
        current = duty_at(person, slot, day)
        if current is not None:
            if current.slot_id != slot.pk:
                raise BoardError(
                    f"{person.short_name}: слот занят ведением группы из слота {current.slot} — "
                    "измените его там."
                )
            _end(user, current, day)
        duty = InstructorDuty(
            instructor=person,
            slot=slot,
            kind=kind,
            label=label.strip() if kind == BlockKind.OTHER else "",
            group_session=session if kind == BlockKind.GROUP_LEAD else None,
            valid_from=day,
            valid_to=until,
        )
        _full_clean(duty)
        duty._history_user = user
        duty.save()
        result.append(duty)
    return result


@transaction.atomic
def end_duty(
    user: User, instructor: Instructor, slot: InstructorSlot, day: date, *, with_partner: bool
) -> list[str]:
    """Завершить распорядок в ячейке с этой даты: последний день — накануне; если он и
    начинался с этой даты — удаляется. Возвращает, у кого завершено."""
    _require_board(user)
    people = _people(instructor, with_partner)
    list(
        Instructor.objects.select_for_update().filter(pk__in=[p.pk for p in people]).order_by("pk")
    )
    done = []
    for person in people:
        duty = duty_at(person, slot, day)
        if duty is None:
            continue
        _end(user, duty, day)
        done.append(person.short_name)
    if not done:
        raise BoardError("В ячейке нет постоянного распорядка.")
    return done


def _end(user: User, duty: InstructorDuty, day: date) -> None:
    duty._history_user = user
    if duty.valid_from >= day:
        duty.delete()
        return
    duty.valid_to = day - timedelta(days=1)
    duty.save(update_fields=["valid_to"])


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


# --- Экран «Распорядок»: клик по ячейке, как в календаре смен (FR-STF-5) --------------------

# «Кисть» экрана «Распорядок»: вид занятости или «Свободно».
FREE = "FREE"


def paint_duty(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    *,
    once: bool,
    label: str = "",
    group_session: GroupSession | None = None,
) -> str:
    """Клик по ячейке экрана «Распорядок» выбранной «кистью». Возвращает, что сделано.

    Постоянно (``once=False``): вид ставится с даты бессрочно, прежний в слоте заканчивается
    накануне; клик тем же видом или «Свободно» снимает распорядок с этой даты.
    Только на дату (``once=True``): ставится разовый блок; клик тем же видом убирает блок —
    слот снова идёт по распорядку; «Свободно» освобождает слот на эту дату.
    Правила — те же, что у панели ячейки шахматки: функции ниже общие.
    """
    _require_board(user)
    if kind != FREE:
        _check_duty_fields(kind, label, group_session)
        _check_in_shift(instructor, slot, day, group_session)
    if once:
        return _paint_once(user, instructor, slot, day, kind, label, group_session)
    current = duty_at(instructor, slot, day)
    if kind == FREE or (current is not None and _same(current, kind, label, group_session)):
        end_duty(user, instructor, slot, day, with_partner=False)
        return f"{instructor.short_name}: распорядок снят с {day:%d.%m.%Y}."
    add_duty(user, instructor, slot, day, kind, label, group_session=group_session)
    return f"{instructor.short_name}: распорядок с {day:%d.%m.%Y}."


def _paint_once(
    user: User,
    instructor: Instructor,
    slot: InstructorSlot,
    day: date,
    kind: str,
    label: str,
    session: GroupSession | None,
) -> str:
    block = InstructorBlock.objects.filter(instructor=instructor, slot=slot, date=day).first()
    on_date = f"{instructor.short_name} на {day:%d.%m.%Y}"
    if block is not None and (
        _same(block, kind, label, session) or (kind == FREE and block.kind == BlockKind.CLEAR)
    ):
        _delete_block(user, block)
        return f"{on_date}: разовый блок убран, слот идёт по распорядку."
    if kind != FREE:
        result = add_block(user, instructor, slot, day, kind, label, group_session=session)
        if result.skipped:
            raise BoardError(
                f"{on_date}: в ячейке стоит пациент — сначала перенесите его в шахматке."
            )
        return f"{on_date}: разовый блок поставлен."
    if block is not None:
        _delete_block(user, block)
    busy = instructor_calendars([instructor], day, day)[instructor.pk].plan_on(day)
    if slot.pk in busy:
        clear_block(user, instructor, slot, day)
    return f"{on_date}: слот свободен."


@transaction.atomic
def _delete_block(user: User, block: InstructorBlock) -> None:
    _require_board(user)
    block._history_user = user
    block.delete()


def _same(
    item: InstructorDuty | InstructorBlock, kind: str, label: str, session: GroupSession | None
) -> bool:
    """Та же занятость, что выбрана «кистью»: вид, группа, подпись «прочего»."""
    if item.kind != kind:
        return False
    if kind == BlockKind.GROUP_LEAD:
        return session is not None and item.group_session_id == session.pk
    if kind == BlockKind.OTHER:
        return item.label.strip() == label.strip()
    return True


def _check_in_shift(
    instructor: Instructor, slot: InstructorSlot, day: date, session: GroupSession | None
) -> None:
    """Распорядок — только в часы смены: 2/2 — 8:00–20:00, остальные — 8:00–17:00."""
    rule = instructor_calendars([instructor], day, day)[instructor.pk].pattern_on(day)
    if rule is None:
        raise BoardError(
            f"{instructor.short_name}: на {day:%d.%m.%Y} нет шаблона смены — "
            "задайте его на экране «Смены»."
        )
    start, end = (f"{t.hour}:{t:%M}" for t in staff_domain.shift_hours(rule.pattern))
    for item in _covered(slot, session):
        if not staff_domain.slot_in_shift(rule.pattern, item.start, item.end):
            raise BoardError(
                f"{instructor.short_name} работает с {start} до {end}: слот {item} вне смены."
            )
