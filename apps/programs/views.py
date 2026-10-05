from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.accounts.access import current_department, is_admin, role_in
from apps.accounts.models import Role
from apps.scheduling.services import can_schedule, program_schedule

from . import services
from .forms import PrescriptionEditForm, PrescriptionForm, ProgramForm
from .models import Prescription, Program, ProgramSource
from .services import Period, ProgramsError


def program_list(request: HttpRequest) -> HttpResponse:
    department = current_department(request)
    if department is None:
        return render(request, "no_department.html")
    try:
        period = Period(request.GET.get("period", Period.CURRENT))
    except ValueError:
        period = Period.CURRENT
    query = request.GET.get("q", "")
    return render(
        request,
        "programs/program_list.html",
        {
            "programs": services.programs_for(request.user, department, period=period, query=query),
            "period": period,
            "periods": [
                (Period.CURRENT, "текущие"),
                (Period.FINISHED, "завершённые"),
                (Period.ALL, "все"),
            ],
            "query": query,
            "can_create": services.can_create(request.user, department),
        },
    )


def program_create(request: HttpRequest) -> HttpResponse:
    """Запасной путь — без листа назначений (FR-PRG-5)."""
    department = current_department(request)
    if department is None or not services.can_create(request.user, department):
        raise PermissionDenied("Создавать программы может врач отделения.")
    program = Program(department=department, start_date=date.today(), source=ProgramSource.MANUAL)
    if role_in(request.user, department) == Role.DOCTOR:
        program.attending_doctor = request.user
    return _program_form(request, program, "Новая программа вручную")


def program_edit(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not services.can_edit(request.user, program):
        raise PermissionDenied("Данные программы правит врач отделения.")
    return _program_form(request, program, "Данные пациента и курса")


def _program_form(request: HttpRequest, program: Program, title: str) -> HttpResponse:
    form = ProgramForm(request.POST or None, instance=program, department=program.department)
    if request.method == "POST" and form.is_valid():
        try:
            saved = services.save_program(
                request.user, form.instance, end_date_changed="end_date" in form.changed_data
            )
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Сохранено.")
            return redirect("programs:detail", pk=saved.pk)
    return render(
        request, "programs/program_form.html", {"form": form, "program": program, "title": title}
    )


def program_detail(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    return render(
        request,
        "programs/program_detail.html",
        {
            "program": program,
            "review_items": services.review_items(program),
            "schedule": program_schedule(program),
            "can_schedule": can_schedule(request.user, program),
            "can_delete": is_admin(request.user),
            **_prescriptions_context(request, program),
        },
    )


def program_delete(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not is_admin(request.user):
        raise PermissionDenied("Удалять программы может только администратор.")
    if request.method == "POST":
        name = program.full_name
        services.delete_program(request.user, program)
        messages.success(request, f"Программа «{name}» удалена.")
        return redirect("programs:list")
    return render(
        request,
        "programs/program_delete.html",
        {
            "program": program,
            "prescription_count": program.prescriptions.count(),
            "booking_count": program.bookings.count(),
        },
    )


@require_POST
def program_dismiss_warnings(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    services.dismiss_warnings(request.user, program)
    return redirect("programs:detail", pk=pk)


# --- Назначения (HTMX: каждое действие возвращает обновлённую таблицу) --------------------


@require_POST
def prescription_add(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    form = PrescriptionForm(request.POST, instance=Prescription(program=program))
    if form.is_valid():
        try:
            services.add_prescription(request.user, form.instance)
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            form = None
    return _prescriptions_response(request, program, add_form=form)


def prescription_edit(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    services.require_editable(request.user, program)
    # Префикс: на странице одновременно форма правки строки и форма добавления.
    form = PrescriptionEditForm(request.POST or None, instance=prescription, prefix="edit")
    if request.method == "POST" and form.is_valid():
        try:
            services.update_prescription(request.user, form.instance)
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            return _prescriptions_response(request, program)
    return _prescriptions_response(request, program, edit_form=form, editing=prescription)


@require_POST
def prescription_delete(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    _run(request, services.delete_prescription, request.user, prescription)
    return _prescriptions_response(request, program)


@require_POST
def prescription_move(
    request: HttpRequest, pk: int, prescription_pk: int, direction: str
) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    step = {"up": -1, "down": 1}.get(direction)
    if step is None:
        raise Http404
    _run(request, services.move_prescription, request.user, prescription, step)
    return _prescriptions_response(request, program)


def _get_prescription(
    request: HttpRequest, pk: int, prescription_pk: int
) -> tuple[Program, Prescription]:
    program = services.get_program_or_404(request.user, pk)
    prescription = get_object_or_404(
        Prescription.objects.select_related("procedure", "program__department"),
        pk=prescription_pk,
        program=program,
    )
    return program, prescription


def _run(request: HttpRequest, action, *args) -> None:
    try:
        action(*args)
    except ProgramsError as error:
        messages.error(request, str(error))


def _prescriptions_context(request, program, *, add_form=None, edit_form=None, editing=None):
    can_edit = services.can_edit(request.user, program)
    if add_form is None and can_edit:
        add_form = PrescriptionForm(instance=Prescription(program=program))
    return {
        "program": program,
        "prescriptions": program.prescriptions.select_related("procedure"),
        "can_edit": can_edit,
        "add_form": add_form,
        "edit_form": edit_form,
        "editing": editing,
    }


def _prescriptions_response(request, program, **kwargs) -> HttpResponse:
    """Таблица назначений и — вне основной цели HTMX (hx-swap-oob) — блок расписания:
    изменение назначения сразу пересобирает расписание, экран должен это показать."""
    program.refresh_from_db()
    table = render_to_string(
        "programs/_prescriptions.html",
        _prescriptions_context(request, program, **kwargs),
        request=request,
    )
    schedule = render_to_string(
        "scheduling/_schedule.html",
        {
            "program": program,
            "schedule": program_schedule(program),
            "can_schedule": can_schedule(request.user, program),
            "oob": True,
        },
        request=request,
    )
    return HttpResponse(table + schedule)
