"""Импорт листа назначений в программу (TZ.md §6.3) и обновление программы из новой версии
листа (FR-IMP-15)."""

from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from enum import StrEnum

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef

from apps.accounts.access import require_right
from apps.accounts.models import Right, User
from apps.catalog.models import SESSION_KINDS, Procedure, ProcedureKind
from apps.catalog.services import visible_procedures
from apps.org.models import Department
from apps.programs.domain import SHRM_LABELS, course_end, patient_key, sex_from_name
from apps.programs.models import Prescription, Program, ProgramSource, Withdrawal
from apps.programs.services import (
    ProgramsError,
    can_edit,
    copy_plan,
    find_previous_courses,
    require_editable,
    save_program,
)
from apps.scheduling import board_days, journal
from apps.scheduling.services import replan

from .matching import ProcedureRef, match_with_typos
from .prescription_sheet import Sheet, SheetRow

MAX_FILE_SIZE = 5 * 1024 * 1024


class DuplicateProgramError(Exception):
    """Похожая программа уже есть (FR-IMP-11). Врач может создать новую явно."""

    def __init__(self, programs: list[Program]):
        super().__init__("Похожая программа уже есть.")
        self.programs = programs


class PreviousCoursesFound(Exception):
    """Пациент уже лечился в отделении (FR-IMP-13): врач выбирает, по какому плану создать."""

    def __init__(self, programs: list[Program]):
        super().__init__("У пациента есть прошлые курсы.")
        self.programs = programs


class Plan(StrEnum):
    """Из чего создать программу, если у пациента есть прошлый курс (FR-PRG-10)."""

    SHEET = "sheet"  # по новому листу, связать с прошлым курсом
    PREVIOUS = "previous"  # назначения прошлого курса, шапка — из нового листа
    OTHER = "other"  # это другой пациент — без связи


@dataclass(frozen=True)
class PlanLine:
    """Строка сравнения назначений: что было в прошлом курсе и что в новом листе."""

    label: str
    old: str = ""
    new: str = ""

    @property
    def state(self) -> str:
        if self.old and self.new:
            return "same" if self.old == self.new else "changed"
        return "added" if self.new else "removed"


def find_duplicates(department: Department, sheet: Sheet, start: date) -> list[Program]:
    """Программы отделения того же пациента, курс которых пересекается с курсом из листа
    (FR-IMP-11). Тот же пациент — совпал ИБ, либо у одной из программ ИБ нет, а ФИО
    совпадает без учёта регистра, «ё» и лишних пробелов. Разные ИБ — разные курсы."""
    end = course_end(start, department.course_length(sheet.shrm))
    name = patient_key(sheet.full_name)
    # Выбывший до начала нового курса — это прошлый курс, а не дубль (FR-IMP-13).
    overlapping = (
        Program.objects.filter(department=department, start_date__lte=end, end_date__gte=start)
        .exclude(
            Exists(
                Withdrawal.objects.filter(
                    program=OuterRef("pk"), returned_on__isnull=True, date_from__lte=start
                )
            )
        )
        .order_by("start_date")
    )
    duplicates = []
    for program in overlapping:
        if sheet.history_number and program.history_number:
            same = program.history_number == sheet.history_number
        else:
            same = patient_key(program.full_name) == name
        if same:
            duplicates.append(program)
    return duplicates


def compare_plans(department: Department, sheet: Sheet, previous: Program) -> list[PlanLine]:
    """Назначения прошлого курса и нового листа рядом (FR-IMP-13): по процедуре, в порядке
    нового листа, затем то, что было только в прошлом курсе."""
    old: dict[object, str] = {}
    labels: dict[object, str] = {}
    for item in previous.prescriptions.select_related("procedure").order_by("card_order", "pk"):
        key = item.procedure_id or f"raw:{item.raw_text}"
        old[key] = _dose(item.duration_min, item.per_day)
        labels[key] = item.procedure.card_label if item.procedure else item.raw_text
    lines = []
    for row, procedure, duration in _matched_rows(department, sheet):
        key = procedure.pk if procedure else f"raw:{row.raw_text}"
        label = procedure.card_label if procedure else row.raw_text
        lines.append(PlanLine(label, old.pop(key, ""), _dose(duration, row.per_day)))
    lines += [PlanLine(labels[key], dose, "") for key, dose in old.items()]
    return lines


def _dose(duration_min: int | None, per_day: int) -> str:
    """«30 мин, 1 р/д» — по этому видно, что назначение изменилось."""
    return ", ".join(([f"{duration_min} мин"] if duration_min else []) + [f"{per_day} р/д"])


@transaction.atomic
@journal.logged("Импорт листа назначений")
def import_prescription_sheet(
    user: User,
    department: Department,
    sheet: Sheet,
    *,
    attending_doctor: User,
    force: bool = False,
    plan: Plan | None = None,
    previous: Program | None = None,
    today: date | None = None,
) -> Program:
    """Создаёт программу из листа назначений и сразу заполняет назначения (FR-IMP-10).

    Всё, что требует внимания врача, записывается в import_warnings и показывается
    на странице программы — отдельного экрана подтверждения нет. Исключение — похожая
    программа (FR-IMP-11) и прошлые курсы пациента (FR-IMP-13): пока ``plan`` не выбран,
    создание останавливается с ``PreviousCoursesFound``.
    """
    require_right(user, Right.PROGRAMS_EDIT, department)
    warnings = list(sheet.warnings)

    start = sheet.start_date
    if start is None:
        start = today or date.today()
        warnings.append(
            f"В листе нет дат назначений — начало курса взято сегодняшним днём "
            f"({start:%d.%m.%Y}). Проверьте его."
        )
    if not force:
        duplicates = find_duplicates(department, sheet, start)
        if duplicates:
            raise DuplicateProgramError(duplicates)
    if plan is None:
        courses = find_previous_courses(department, sheet.full_name, start)
        if courses:
            raise PreviousCoursesFound(courses)
    elif plan != Plan.OTHER:
        if previous is None or previous.department_id != department.pk:
            raise ValidationError("Выберите прошлый курс пациента из этого отделения.")
    if plan == Plan.OTHER:
        previous = None

    sex = sex_from_name(sheet.full_name)
    if not sex:
        warnings.append("Пол не определился по отчеству — укажите его в данных пациента.")

    program = Program(
        department=department,
        full_name=sheet.full_name,
        sex=sex,
        history_number=sheet.history_number,
        room=sheet.room,
        shrm=sheet.shrm,
        attending_doctor=attending_doctor,
        start_date=start,
        source=ProgramSource.IMPORT,
        previous=previous,
    )
    save_program(user, program, end_date_changed=False)

    if plan == Plan.PREVIOUS:
        copy_plan(user, program, previous)
        warnings.append(
            f"Назначения взяты из прошлого курса ({previous.start_date:%d.%m.%Y} – "
            f"{previous.end_date:%d.%m.%Y}), а не из листа — проверьте их."
        )
    else:
        warnings += _create_prescriptions(user, program, sheet)
    program.import_warnings = warnings
    program._history_user = user
    program.save(update_fields=["import_warnings", "updated_at"])
    # Программа сразу получает расписание групп, бассейна и тренажёров (TZ.md §1.1).
    replan(program)
    return program


def _matched_rows(
    department: Department, sheet: Sheet, warnings: list[str] | None = None
) -> list[tuple]:
    """Строки листа, которые станут назначениями: строка, процедура (или None), длительность.

    Процедура «только строка в карте» встречается в карте один раз: «Индивид. психолог» и
    «Консультация психолога» — одна строка «Психолог» (решение 71). Опечатка в одну букву
    прощается, но врач получает предупреждение в ``warnings``.
    """
    procedures = {p.pk: p for p in visible_procedures(department)}
    refs = [
        ProcedureRef(p.pk, p.name, p.kind, tuple(p.synonyms), p.group_choice)
        for p in procedures.values()
    ]
    result: list[tuple] = []
    card_only: set[int] = set()
    for row in sheet.rows:
        # Консультации — только те, что есть в карте (психолог, логопед, эрготерапевт);
        # «терапевт» и прочие пропускаем молча (FR-IMP-5).
        kinds = (ProcedureKind.CARD_ONLY,) if row.consultation else None
        found = match_with_typos(row.match_text, refs, kinds=kinds)
        if found.procedure is None and row.consultation:
            continue
        procedure: Procedure | None = procedures[found.procedure.id] if found.procedure else None
        if procedure and procedure.kind == ProcedureKind.CARD_ONLY:
            if procedure.pk in card_only:
                continue
            card_only.add(procedure.pk)
        if found.typo and procedure and warnings is not None:
            warnings.append(
                f"«{row.raw_text}» распознано как «{procedure.name}» — похоже на опечатку, "
                "проверьте."
            )
        if procedure and procedure.kind == ProcedureKind.INDIVIDUAL and row.number:
            # «Индивидуальное занятие 30 мин №2» — два занятия в день (решение 71).
            row = replace(row, per_day=row.number)
        duration = row.duration_min or (procedure.default_duration_min if procedure else None)
        result.append((row, procedure, duration))
    return result


def _create_prescriptions(user: User, program: Program, sheet: Sheet) -> list[str]:
    warnings = []
    for order, (row, procedure, duration) in enumerate(
        _matched_rows(program.department, sheet, warnings), start=1
    ):
        prescription = Prescription(program=program, procedure=procedure)
        warnings += _fill_from_row(prescription, row, duration, order)
        prescription._history_user = user
        prescription.save()
    return warnings


def _fill_from_row(
    prescription: Prescription, row: SheetRow, duration: int | None, order: int
) -> list[str]:
    """Поля назначения из строки листа — одинаково при импорте и обновлении (FR-IMP-15)."""
    program, procedure = prescription.program, prescription.procedure
    prescription.raw_text = row.raw_text
    prescription.duration_min = duration
    prescription.per_day = row.per_day
    prescription.start_date = (
        row.prescribed_on if row.prescribed_on and row.prescribed_on > program.start_date else None
    )
    prescription.cancel_date = row.cancel_date
    prescription.in_card = not (procedure and procedure.evening_individual)
    prescription.card_order = order
    return _group_time(prescription, row) + _fit_into_course(prescription)


def _group_time(prescription: Prescription, row: SheetRow) -> list[str]:
    """«Спина в 16:30»: время из листа — выбор занятия группы на весь курс (решение 81).
    Строка без времени выбор не трогает: его могли сделать на странице программы. Выбор,
    который не подходит к процедуре (врач сменил группу), сбрасывается."""
    if prescription.group_session_id and not prescription.session_fits():
        prescription.group_session = None
    procedure = prescription.procedure
    if not row.at or procedure is None or procedure.kind not in SESSION_KINDS:
        return []
    if procedure.is_generic_pool and prescription.pool_group is None:
        return [
            f"«{row.raw_text}»: время из листа ({row.at}) не учтено — сначала выберите группу "
            "бассейна в блоке «Время групп», затем время."
        ]
    group = prescription.choice_group or procedure
    at = time.fromisoformat(row.at)
    sessions = sorted(group.sessions.filter(is_active=True), key=lambda s: s.start_time)
    found = next((s for s in sessions if s.start_time == at), None)
    if found is not None:
        prescription.group_session = found
        return []
    known = ", ".join(f"{s.start_time:%H:%M}" for s in sessions) or "нет занятий"
    return [
        f"«{row.raw_text}»: у «{group.name}» нет занятия в {row.at} (есть: {known}) — "
        "время из листа не учтено. Проверьте назначение."
    ]


def _fit_into_course(prescription: Prescription) -> list[str]:
    """Даты и частота из листа могут не помещаться в правила программы. Импорт не падает:
    значение исправляется, а врач получает предупреждение."""
    try:
        prescription.full_clean()
        return []
    except ValidationError as error:
        fields = error.message_dict
    label = prescription.raw_text
    warnings = []
    if "start_date" in fields:
        prescription.start_date = None
        warnings.append(f"«{label}»: дата назначения вне курса — считается с начала курса.")
    if "cancel_date" in fields:
        too_early = prescription.cancel_date <= prescription.effective_start
        prescription.cancel_date = None
        reason = "не позже начала процедуры" if too_early else "после окончания курса"
        warnings.append(
            f"«{label}»: дата отмены {reason} — не учтена, процедура стоит на весь курс. "
            "Проверьте дату."
        )
    if "duration_min" in fields:
        prescription.duration_min = None
        warnings.append(f"«{label}»: длительность не подходит — не учтена.")
    if "per_day" in fields and prescription.procedure:
        limit = prescription.program.department.max_individual_per_day
        warnings.append(
            f"«{label}»: {prescription.per_day} р/д больше лимита — поставлено {limit}."
        )
        prescription.per_day = limit
    # Исправили — проверяем ещё раз: сохранять недопустимое нельзя даже с предупреждением.
    prescription.full_clean()
    return warnings


# --- Обновление программы из новой версии листа (FR-IMP-15) ----------------------------------

# Поля шапки, которые берутся из нового листа. Начало курса не сдвигается (решение 69).
UPDATE_HEADER = (
    ("full_name", "ФИО"),
    ("history_number", "ИБ"),
    ("room", "Палата"),
    ("shrm", "ШРМ"),
)
LINE_STATES = {
    "same": "без изменений",
    "changed": "изменено",
    "added": "добавлено",
    "cancelled": "отменено",
    "deleted": "удалено",
    "kept": "уже отменено",
}


@dataclass(frozen=True)
class HeaderChange:
    label: str
    old: str
    new: str


@dataclass(frozen=True)
class UpdateLine:
    """Назначение на экране изменений: «было» и «стало» (FR-IMP-15)."""

    label: str
    old: str
    new: str
    state: str

    @property
    def state_text(self) -> str:
        return LINE_STATES[self.state]


@dataclass
class SheetUpdate:
    header: list[HeaderChange]
    lines: list[UpdateLine]
    warnings: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.header) or any(line.state not in ("same", "kept") for line in self.lines)


def can_update_from_sheet(user: User, program: Program, today: date | None = None) -> bool:
    """Обновлять из листа можно, пока программу можно править и курс не закончился."""
    return can_edit(user, program) and not program.is_finished(today or board_days.today())


def preview_sheet_update(
    user: User, program: Program, sheet: Sheet, *, today: date | None = None
) -> SheetUpdate:
    """Что изменится в программе, если применить лист. Ничего не сохраняет."""
    program = Program.objects.select_related("department").get(pk=program.pk)
    today = today or board_days.today()
    _require_updatable(user, program, today)
    update, _saves, _deletes = _merge(program, sheet, today)
    return update


@transaction.atomic
@journal.logged("Обновление из новой версии листа назначений")
def apply_sheet_update(
    user: User, program: Program, sheet: Sheet, *, today: date | None = None
) -> SheetUpdate:
    """Обновляет программу из новой версии листа (FR-IMP-15, решение 69): шапка и назначения
    — из листа; строки обновляются, а не пересоздаются, чтобы их прошедшие занятия, группа
    бассейна, закреплённые занятия и место в шахматке остались."""
    program = (
        Program.objects.select_for_update(of=("self",))
        .select_related("department")
        .get(pk=program.pk)
    )
    today = today or board_days.today()
    _require_updatable(user, program, today)
    update, saves, deletes = _merge(program, sheet, today)
    for item in deletes:
        item._history_user = user
        item.delete()
    for item in saves:
        item._history_user = user
        item.save()
    program.import_warnings = list(dict.fromkeys([*program.import_warnings, *update.warnings]))
    # Окончание курса уже посчитано в _merge; save_program проверит назначения и пересоберёт
    # расписание (replan).
    save_program(user, program, end_date_changed=False)
    return update


def _require_updatable(user: User, program: Program, today: date) -> None:
    require_editable(user, program)
    if program.is_finished(today):
        raise ProgramsError("Курс уже закончился — обновлять программу из листа поздно.")


def _merge(
    program: Program, sheet: Sheet, today: date
) -> tuple[SheetUpdate, list[Prescription], list[Prescription]]:
    """Применяет лист к программе в памяти: что сохранить, что удалить и как это показать.
    Предпросмотр и применение идут через одну функцию — экран не может разойтись с делом."""
    warnings = list(sheet.warnings)
    if program.history_number and sheet.history_number not in ("", program.history_number):
        warnings.append(
            f"В листе ИБ № {sheet.history_number}, в программе — № {program.history_number}. "
            "Проверьте, что это лист того же пациента."
        )
    if patient_key(sheet.full_name) != patient_key(program.full_name):
        warnings.append(
            f"В листе ФИО «{sheet.full_name}», в программе — «{program.full_name}». "
            "Проверьте, что это лист того же пациента."
        )
    if sheet.start_date and sheet.start_date != program.start_date:
        warnings.append(
            f"В листе курс начинается {sheet.start_date:%d.%m.%Y}, в программе — "
            f"{program.start_date:%d.%m.%Y}. Начало идущего курса при обновлении не меняется; "
            "если нужно — поправьте его в «Данных пациента и курса»."
        )

    header = []
    for field, label in UPDATE_HEADER:
        old, new = getattr(program, field), getattr(sheet, field)
        if old != new:
            shown = (SHRM_LABELS[old], SHRM_LABELS[new]) if field == "shrm" else (old, new)
            header.append(HeaderChange(label, *(str(value) or "—" for value in shown)))
            setattr(program, field, new)
    program.sex = sex_from_name(program.full_name) or program.sex
    if not program.end_date_manual:
        planned = program.planned_end_date()
        if planned != program.end_date:
            header.append(
                HeaderChange(
                    "Окончание курса", f"{program.end_date:%d.%m.%Y}", f"{planned:%d.%m.%Y}"
                )
            )
            program.end_date = planned

    existing = list(
        program.prescriptions.select_related("procedure", "pool_group", "group_session").order_by(
            "card_order", "pk"
        )
    )
    for item in existing:
        item.program = program  # проверки дат — по новому курсу
    rows = _matched_rows(program.department, sheet, warnings)
    pairs = _pair_rows(rows, existing)

    lines: list[UpdateLine] = []
    saves: list[Prescription] = []
    for order, ((row, procedure, duration), item) in enumerate(
        zip(rows, pairs, strict=True), start=1
    ):
        old = f"{item.label}: {_describe(item)}" if item else ""
        if item is None:
            item = Prescription(program=program, procedure=procedure)
        elif procedure is not None:
            item.procedure = procedure
        # Если лист снова не распознал строку, остаётся процедура, выбранная врачом в вебе.
        if duration is None and item.procedure:
            duration = item.procedure.default_duration_min
        warnings += _fill_from_row(item, row, duration, order)
        new = f"{item.label}: {_describe(item)}"
        state = "added" if not old else "same" if old == new else "changed"
        lines.append(UpdateLine(item.label, _strip(old, item), _strip(new, item), state))
        saves.append(item)

    deletes: list[Prescription] = []
    tomorrow = today + timedelta(days=1)
    used = {item.pk for item in pairs if item}
    for item in existing:
        if item.pk in used:
            continue
        old = _describe(item)
        if item.effective_start > today:
            deletes.append(item)
            lines.append(UpdateLine(item.label, old, "", "deleted"))
            continue
        if (item.cancel_date and item.cancel_date <= tomorrow) or tomorrow > program.end_date:
            state = "kept"  # уже отменено или курс кончается сегодня — отменять нечего
        else:
            item.cancel_date = tomorrow
            state = "cancelled"
        item.card_order = len(saves) + 1
        warnings += _fit_into_course(item)
        lines.append(UpdateLine(item.label, old, _describe(item), state))
        saves.append(item)
    return SheetUpdate(header, lines, warnings), saves, deletes


def _pair_rows(rows: list[tuple], existing: list[Prescription]) -> list[Prescription | None]:
    """Строке листа — назначение программы: сначала тот же текст строки (врач мог выбрать
    процедуру нераспознанной строке), затем та же процедура."""
    free = list(existing)
    pairs: list[Prescription | None] = [None] * len(rows)
    for index, (row, _procedure, _duration) in enumerate(rows):
        text = " ".join(row.raw_text.split())
        item = next((p for p in free if " ".join(p.raw_text.split()) == text), None)
        if item:
            free.remove(item)
            pairs[index] = item
    for index, (_row, procedure, _duration) in enumerate(rows):
        if pairs[index] is None and procedure is not None:
            item = next((p for p in free if p.procedure_id == procedure.pk), None)
            if item:
                free.remove(item)
                pairs[index] = item
    return pairs


def _describe(item: Prescription) -> str:
    """«30 мин, 1 р/д, с 05.10, отмена 09.10» — по этому видно, что изменилось."""
    parts = [_dose(item.duration_min, item.per_day)]
    if item.start_date:
        parts.append(f"с {item.start_date:%d.%m}")
    if item.cancel_date:
        parts.append(f"отмена {item.cancel_date:%d.%m}")
    if item.group_session:
        parts.append(f"в {item.group_session.start_time:%H:%M}")
    if not item.in_card:
        parts.append("не в карте")
    return ", ".join(parts)


def _strip(text: str, item: Prescription) -> str:
    """Название процедуры показывается отдельно — в «было/стало» оно нужно, только если
    изменилось."""
    prefix = f"{item.label}: "
    return text.removeprefix(prefix)
