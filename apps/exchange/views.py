import secrets

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.access import current_department, get_department_or_404, select_department
from apps.accounts.models import User
from apps.programs.services import ProgramsError

from .forms import SheetUploadForm, import_departments
from .prescription_sheet import Sheet, SheetError, parse_sheet
from .services import DuplicateProgramError, import_prescription_sheet

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
    """«Всё равно создать новую» после предупреждения о похожей программе (FR-IMP-11)."""
    pending_all = request.session.get(PENDING_KEY, {})
    pending = pending_all.pop(request.POST.get("token", ""), None)
    request.session[PENDING_KEY] = pending_all
    try:
        department = get_department_or_404(request.user, pending["department"])
        doctor = User.objects.get(pk=pending["doctor"])
        sheet = Sheet.from_dict(pending["sheet"])
    # Нет записи, её формат устарел после обновления системы, врача удалили, доступ к отделению
    # пропал — для пользователя всё это одно и то же.
    except (TypeError, KeyError, ValueError, User.DoesNotExist, Http404):
        messages.error(request, "Загрузка устарела — выберите файл ещё раз.")
        return redirect("exchange:sheet_import")
    return _create(request, department, sheet, doctor, force=True)


def _create(request, department, sheet: Sheet, doctor: User, *, force: bool) -> HttpResponse:
    try:
        program = import_prescription_sheet(
            request.user, department, sheet, attending_doctor=doctor, force=force
        )
    except (ProgramsError, ValidationError) as error:
        text = "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)
        messages.error(request, f"Программа не создана: {text}")
        return redirect("exchange:sheet_import")
    except DuplicateProgramError as error:
        token = secrets.token_urlsafe(16)
        pending_all = request.session.get(PENDING_KEY, {})
        pending_all[token] = {
            "department": department.pk,
            "doctor": doctor.pk,
            "sheet": sheet.to_dict(),
        }
        # Держим только последние загрузки — сессия не должна расти бесконечно.
        request.session[PENDING_KEY] = dict(list(pending_all.items())[-MAX_PENDING:])
        return render(
            request,
            "exchange/sheet_duplicate.html",
            {"sheet": sheet, "duplicates": error.programs, "token": token},
        )
    # Список программ в шапке — того отделения, куда легла новая программа.
    select_department(request, department.pk)
    messages.success(request, f"Программа создана из листа назначений ({department.name}).")
    return redirect("programs:detail", pk=program.pk)
