import secrets

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.access import current_department, get_department_or_404, select_department
from apps.accounts.models import User
from apps.programs.models import Program
from apps.programs.services import ProgramsError, get_program_or_404

from .forms import SheetFileForm, SheetUploadForm, import_departments
from .prescription_sheet import Sheet, SheetError, parse_sheet
from .services import (
    DuplicateProgramError,
    Plan,
    PreviousCoursesFound,
    apply_sheet_update,
    can_update_from_sheet,
    compare_plans,
    import_prescription_sheet,
    preview_sheet_update,
)

# Разобранный лист ждёт решения врача «всё равно создать» в сессии, а не на диске:
# исходный файл не хранится (FR-IMP-12). Каждая загрузка — под своим ключом, чтобы
# две вкладки с разными листами не перепутались.
PENDING_KEY = "pending_prescription_sheets"
MAX_PENDING = 5


def sheet_import(request: HttpRequest) -> HttpResponse:
    """Загрузка листа назначений → новая программа (TZ.md §6)."""
    if not import_departments(request.user).exists():
        raise PermissionDenied("Загружать листы назначений может врач отделения.")

    form = SheetUploadForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        initial_department=current_department(request),
    )
    if request.method == "POST" and form.is_valid():
        try:
            sheet = parse_sheet(form.cleaned_data["file"])
        except SheetError as error:
            form.add_error("file", str(error))
        else:
            department = form.cleaned_data["department"]
            return _create(
                request, department, sheet, form.cleaned_data["chosen_doctor"], force=False
            )
    return render(request, "exchange/sheet_import.html", {"form": form})


@require_POST
def sheet_import_force(request: HttpRequest) -> HttpResponse:
    """Продолжить загрузку после вопроса: «Всё равно создать новую» при похожей программе
    (FR-IMP-11) или выбор плана при прошлом курсе (FR-IMP-13, ``plan``)."""
    pending_all = request.session.get(PENDING_KEY, {})
    pending = pending_all.pop(request.POST.get("token", ""), None)
    request.session[PENDING_KEY] = pending_all
    try:
        department = get_department_or_404(request.user, pending["department"])
        doctor = User.objects.get(pk=pending["doctor"])
        sheet = Sheet.from_dict(pending["sheet"])
        # Без выбора плана — это кнопка «Всё равно создать новую» на экране похожей программы.
        force = bool(pending.get("force")) or "plan" not in request.POST
    # Нет записи, её формат устарел после обновления системы, врача удалили, доступ к отделению
    # пропал — для пользователя всё это одно и то же.
    except (TypeError, KeyError, ValueError, User.DoesNotExist, Http404):
        messages.error(request, "Загрузка устарела — выберите файл ещё раз.")
        return redirect("exchange:sheet_import")
    plan = previous = None
    if "plan" in request.POST:
        try:
            plan = Plan(request.POST["plan"])
        except ValueError:
            raise Http404("Неизвестный вариант.") from None
        if plan != Plan.OTHER:
            previous = (
                Program.objects.filter(
                    department=department, pk=request.POST.get("previous")
                ).first()
                if (request.POST.get("previous") or "").isdigit()
                else None
            )
    return _create(request, department, sheet, doctor, force=force, plan=plan, previous=previous)


def _create(
    request,
    department,
    sheet: Sheet,
    doctor: User,
    *,
    force: bool,
    plan: Plan | None = None,
    previous: Program | None = None,
) -> HttpResponse:
    try:
        program = import_prescription_sheet(
            request.user,
            department,
            sheet,
            attending_doctor=doctor,
            force=force,
            plan=plan,
            previous=previous,
        )
    except (ProgramsError, ValidationError) as error:
        text = "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)
        messages.error(request, f"Программа не создана: {text}")
        return redirect("exchange:sheet_import")
    except DuplicateProgramError as error:
        token = _keep(request, department, doctor, sheet, force=False)
        updatable = [p for p in error.programs if can_update_from_sheet(request.user, p)]
        return render(
            request,
            "exchange/sheet_duplicate.html",
            {"sheet": sheet, "duplicates": error.programs, "updatable": updatable, "token": token},
        )
    except PreviousCoursesFound as error:
        token = _keep(request, department, doctor, sheet, force=force)
        courses = [(course, compare_plans(department, sheet, course)) for course in error.programs]
        return render(
            request,
            "exchange/sheet_previous.html",
            {"sheet": sheet, "courses": courses, "token": token},
        )
    # Список программ в шапке — того отделения, куда легла новая программа.
    select_department(request, department.pk)
    messages.success(request, f"Программа создана из листа назначений ({department.name}).")
    return redirect("programs:detail", pk=program.pk)


def _keep(
    request, department, doctor: User, sheet: Sheet, *, force: bool, program: int | None = None
) -> str:
    """Разобранный лист ждёт решения врача в сессии под своим ключом. ``program`` — какую
    программу обновить этим листом (FR-IMP-15)."""
    token = secrets.token_urlsafe(16)
    pending_all = request.session.get(PENDING_KEY, {})
    pending_all[token] = {
        "department": department.pk,
        "doctor": doctor.pk,
        "sheet": sheet.to_dict(),
        "force": force,
        "program": program,
    }
    # Держим только последние загрузки — сессия не должна расти бесконечно.
    request.session[PENDING_KEY] = dict(list(pending_all.items())[-MAX_PENDING:])
    return token


# --- Обновление программы из новой версии листа (FR-IMP-15) ----------------------------------


def sheet_update(request: HttpRequest, pk: int) -> HttpResponse:
    """«Обновить из листа» на странице программы: загрузка файла → экран изменений."""
    program = get_program_or_404(request.user, pk)
    if not can_update_from_sheet(request.user, program):
        raise PermissionDenied("Обновить программу из листа может врач отделения, пока идёт курс.")
    form = SheetFileForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            sheet = parse_sheet(form.cleaned_data["file"])
        except SheetError as error:
            form.add_error("file", str(error))
        else:
            token = _keep(
                request,
                program.department,
                program.attending_doctor,
                sheet,
                force=False,
                program=program.pk,
            )
            return _update_preview(request, program, sheet, token)
    return render(request, "exchange/sheet_update_upload.html", {"form": form, "program": program})


@require_POST
def sheet_update_preview(request: HttpRequest) -> HttpResponse:
    """«Обновить эту программу» на экране повтора: лист уже разобран и ждёт в сессии."""
    token = request.POST.get("token", "")
    pending_all = request.session.get(PENDING_KEY, {})
    try:
        pending = pending_all[token]
        sheet = Sheet.from_dict(pending["sheet"])
        program = get_program_or_404(request.user, int(request.POST.get("program", "")))
    except (TypeError, KeyError, ValueError, Http404):
        messages.error(request, "Загрузка устарела — выберите файл ещё раз.")
        return redirect("exchange:sheet_import")
    pending["program"] = program.pk
    request.session.modified = True
    return _update_preview(request, program, sheet, token)


@require_POST
def sheet_update_apply(request: HttpRequest) -> HttpResponse:
    pending_all = request.session.get(PENDING_KEY, {})
    pending = pending_all.pop(request.POST.get("token", ""), None)
    request.session[PENDING_KEY] = pending_all
    try:
        sheet = Sheet.from_dict(pending["sheet"])
        program = get_program_or_404(request.user, int(pending["program"]))
    except (TypeError, KeyError, ValueError, Http404):
        messages.error(request, "Загрузка устарела — выберите файл ещё раз.")
        return redirect("exchange:sheet_import")
    try:
        apply_sheet_update(request.user, program, sheet)
    except (ProgramsError, ValidationError) as error:
        text = "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)
        messages.error(request, f"Программа не обновлена: {text}")
    else:
        messages.success(request, "Программа обновлена из нового листа назначений.")
    return redirect("programs:detail", pk=program.pk)


def _update_preview(request, program: Program, sheet: Sheet, token: str) -> HttpResponse:
    try:
        update = preview_sheet_update(request.user, program, sheet)
    except ProgramsError as error:
        messages.error(request, str(error))
        return redirect("programs:detail", pk=program.pk)
    return render(
        request,
        "exchange/sheet_update.html",
        {"program": program, "sheet": sheet, "update": update, "token": token},
    )
