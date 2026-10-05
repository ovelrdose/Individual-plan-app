"""Импорт листа назначений в программу (TZ.md §6.3)."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.access import require_role
from apps.accounts.models import Role, User
from apps.catalog.models import ProcedureKind
from apps.catalog.services import visible_procedures
from apps.org.models import Department
from apps.programs.domain import course_end, fold, sex_from_name
from apps.programs.models import Prescription, Program, ProgramSource
from apps.programs.services import save_program
from apps.scheduling.services import replan

from .matching import ProcedureRef, match_procedure
from .prescription_sheet import Sheet

MAX_FILE_SIZE = 5 * 1024 * 1024


class DuplicateProgramError(Exception):
    """Похожая программа уже есть (FR-IMP-11). Врач может создать новую явно."""

    def __init__(self, programs: list[Program]):
        super().__init__("Похожая программа уже есть.")
        self.programs = programs


def find_duplicates(department: Department, sheet: Sheet, start: date) -> list[Program]:
    """Программы отделения того же пациента, курс которых пересекается с курсом из листа
    (FR-IMP-11). Тот же пациент — совпал ИБ, либо у одной из программ ИБ нет, а ФИО
    совпадает без учёта регистра, «ё» и лишних пробелов. Разные ИБ — разные курсы."""
    end = course_end(start, department.course_length(sheet.shrm))
    name = _name_key(sheet.full_name)
    overlapping = Program.objects.filter(
        department=department, start_date__lte=end, end_date__gte=start
    ).order_by("start_date")
    duplicates = []
    for program in overlapping:
        if sheet.history_number and program.history_number:
            same = program.history_number == sheet.history_number
        else:
            same = _name_key(program.full_name) == name
        if same:
            duplicates.append(program)
    return duplicates


def _name_key(full_name: str) -> str:
    return " ".join(fold(full_name).split())


@transaction.atomic
def import_prescription_sheet(
    user: User,
    department: Department,
    sheet: Sheet,
    *,
    attending_doctor: User,
    force: bool = False,
    today: date | None = None,
) -> Program:
    """Создаёт программу из листа назначений и сразу заполняет назначения (FR-IMP-10).

    Всё, что требует внимания врача, записывается в import_warnings и показывается
    на странице программы — отдельного экрана подтверждения нет.
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
    )
    save_program(user, program, end_date_changed=False)

    warnings += _create_prescriptions(user, program, sheet)
    program.import_warnings = warnings
    program._history_user = user
    program.save(update_fields=["import_warnings", "updated_at"])
    # Программа сразу получает расписание групп, бассейна и тренажёров (TZ.md §1.1).
    replan(program)
    return program


def _create_prescriptions(user: User, program: Program, sheet: Sheet) -> list[str]:
    procedures = {p.pk: p for p in visible_procedures(program.department)}
    refs = [ProcedureRef(p.pk, p.name, p.kind, tuple(p.synonyms)) for p in procedures.values()]
    warnings = []
    order = 0
    for row in sheet.rows:
        if row.consultation:
            # Консультации — только те, что есть в карте (психолог, логопед, эрготерапевт);
            # «терапевт» и прочие пропускаем молча (FR-IMP-5).
            ref = match_procedure(row.match_text, refs, kinds=(ProcedureKind.CARD_ONLY,))
            if ref is None:
                continue
        else:
            ref = match_procedure(row.match_text, refs)
        procedure = procedures[ref.id] if ref else None

        order += 1
        prescription = Prescription(
            program=program,
            procedure=procedure,
            raw_text=row.raw_text,
            duration_min=row.duration_min
            or (procedure.default_duration_min if procedure else None),
            per_day=row.per_day,
            start_date=row.prescribed_on
            if row.prescribed_on and row.prescribed_on > program.start_date
            else None,
            cancel_date=row.cancel_date,
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
