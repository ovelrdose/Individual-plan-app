"""Импорт листа назначений в программу (TZ.md §6.3)."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef

from apps.accounts.access import require_role
from apps.accounts.models import Role, User
from apps.catalog.models import Procedure, ProcedureKind
from apps.catalog.services import visible_procedures
from apps.org.models import Department
from apps.programs.domain import course_end, patient_key, sex_from_name
from apps.programs.models import Prescription, Program, ProgramSource, Withdrawal
from apps.programs.services import copy_plan, find_previous_courses, save_program
from apps.scheduling.services import replan

from .matching import ProcedureRef, match_procedure
from .prescription_sheet import Sheet

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
    require_role(user, department, Role.DOCTOR)
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
        diagnosis=sheet.diagnosis,
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


def _matched_rows(department: Department, sheet: Sheet) -> list[tuple]:
    """Строки листа, которые станут назначениями: строка, процедура (или None), длительность."""
    procedures = {p.pk: p for p in visible_procedures(department)}
    refs = [ProcedureRef(p.pk, p.name, p.kind, tuple(p.synonyms)) for p in procedures.values()]
    result: list[tuple] = []
    for row in sheet.rows:
        if row.consultation:
            # Консультации — только те, что есть в карте (психолог, логопед, эрготерапевт);
            # «терапевт» и прочие пропускаем молча (FR-IMP-5).
            ref = match_procedure(row.match_text, refs, kinds=(ProcedureKind.CARD_ONLY,))
            if ref is None:
                continue
        else:
            ref = match_procedure(row.match_text, refs)
        procedure: Procedure | None = procedures[ref.id] if ref else None
        duration = row.duration_min or (procedure.default_duration_min if procedure else None)
        result.append((row, procedure, duration))
    return result


def _create_prescriptions(user: User, program: Program, sheet: Sheet) -> list[str]:
    warnings = []
    for order, (row, procedure, duration) in enumerate(
        _matched_rows(program.department, sheet), start=1
    ):
        prescription = Prescription(
            program=program,
            procedure=procedure,
            raw_text=row.raw_text,
            duration_min=duration,
            per_day=row.per_day,
            start_date=row.prescribed_on
            if row.prescribed_on and row.prescribed_on > program.start_date
            else None,
            cancel_date=row.cancel_date,
            in_card=not (procedure and procedure.evening_individual),
            card_order=order,
        )
        warnings += _fit_into_course(prescription)
        prescription._history_user = user
        prescription.save()
    return warnings


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
