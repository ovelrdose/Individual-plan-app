from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.accounts.access import current_department, is_admin, role_in
from apps.accounts.models import Role
from apps.scheduling import board_days
from apps.scheduling.services import can_schedule, program_schedule

from . import services
from .forms import PrescriptionEditForm, PrescriptionForm, ProgramForm, WithdrawalForm
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
                (Period.WITHDRAWN, "выбывшие"),
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


def program_repeat(request: HttpRequest, pk: int) -> HttpResponse:
    """«Повторный курс» со страницы прошлой программы (FR-PRG-10): данные пациента те же,
    курс — новый, по умолчанию назначения берутся из прошлого курса."""
    previous = services.get_program_or_404(request.user, pk)
    if not services.can_create(request.user, previous.department):
        raise PermissionDenied("Создавать программы может врач отделения.")
    if not previous.is_finished(board_days.today()) and previous.withdrawal is None:
        messages.error(request, "Курс ещё идёт — повторный курс создаётся после его окончания.")
        return redirect("programs:detail", pk=pk)
    doctor = previous.attending_doctor
    if not services.doctors_for(previous.department).filter(pk=doctor.pk).exists():
        doctor = request.user if role_in(request.user, previous.department) == Role.DOCTOR else None
    program = Program(
        department=previous.department,
        full_name=previous.full_name,
        sex=previous.sex,
        age=previous.age,
        room=previous.room,
        shrm=previous.shrm,
        diagnosis=previous.diagnosis,
        attending_doctor=doctor,
        start_date=board_days.today(),
        source=ProgramSource.MANUAL,
    )
    return _program_form(request, program, "Повторный курс", repeat_of=previous)


def program_edit(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not services.can_edit(request.user, program):
        raise PermissionDenied("Данные программы правит врач отделения.")
    return _program_form(request, program, "Данные пациента и курса")


def _program_form(
    request: HttpRequest, program: Program, title: str, repeat_of: Program | None = None
) -> HttpResponse:
    form = ProgramForm(request.POST or None, instance=program, department=program.department)
    # Прошлые курсы пациента (FR-PRG-10): спрашиваем один раз, ответ — поле previous_choice.
    courses: list[Program] = []
    choice = request.POST.get("previous_choice")
    if repeat_of is not None:
        courses = services.find_previous_courses(
            program.department, repeat_of.full_name, program.start_date
        ) or [repeat_of]
        choice = choice or f"copy:{repeat_of.pk}"
    if request.method == "POST" and form.is_valid():
        new = program.pk is None
        if new and choice is None:
            courses = services.find_previous_courses(
                program.department, form.cleaned_data["full_name"], form.cleaned_data["start_date"]
            )
            if courses:
                return _render_program_form(
                    request, form, program, title, courses, f"link:{courses[0].pk}"
                )
        try:
            mode, previous = _previous_choice(request, program, choice) if new else ("", None)
            with transaction.atomic():
                saved = services.save_program(
                    request.user, form.instance, end_date_changed="end_date" in form.changed_data
                )
                if previous is not None:
                    services.link_previous(request.user, saved, previous, copy=mode == "copy")
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Сохранено.")
            return redirect("programs:detail", pk=saved.pk)
    return _render_program_form(request, form, program, title, courses, choice)


def _previous_choice(
    request: HttpRequest, program: Program, choice: str | None
) -> tuple[str, Program | None]:
    """«copy:12» — взять план курса 12, «link:12» — только связать, «other» — без связи."""
    mode, _sep, pk = (choice or "other").partition(":")
    if mode not in ("copy", "link") or not pk.isdigit():
        return "", None
    previous = services.get_program_or_404(request.user, int(pk))
    if previous.department_id != program.department_id:
        raise ProgramsError("Прошлый курс должен быть из того же отделения.")
    return mode, previous


def _render_program_form(request, form, program, title, courses, choice) -> HttpResponse:
    return render(
        request,
        "programs/program_form.html",
        {
            "form": form,
            "program": program,
            "title": title,
            "courses": [(c, f"copy:{c.pk}", f"link:{c.pk}") for c in courses],
            "previous_choice": choice or "",
        },
    )


def program_detail(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    # Индивидуальные на странице — из шахматок: они составляются сами в новый день (FR-SCH-5).
    board_days.ensure_boards()
    return render(
        request,
        "programs/program_detail.html",
        {
            "program": program,
            "review_items": services.review_items(program),
            "schedule": program_schedule(program),
            "can_schedule": can_schedule(request.user, program),
            "can_delete": is_admin(request.user),
            "courses": services.patient_courses(program),
            # Повторный курс — у завершённой или выбывшей программы (FR-PRG-10).
            "can_repeat": services.can_create(request.user, program.department)
            and (program.is_finished(board_days.today()) or program.withdrawal is not None),
            **_withdrawal_context(request, program),
            **_prescriptions_context(request, program),
        },
    )


def _withdrawal_context(request: HttpRequest, program: Program, form=None) -> dict:
    """Блок выбытия (FR-PRG-9): отметка и восстановление — врач и специалист ФР отделения."""
    allowed = services.can_withdraw(request.user, program)
    withdrawal = program.withdrawal
    today = board_days.today()
    return {
        "withdrawal": withdrawal,
        "can_withdraw": allowed
        and withdrawal is None
        and program.start_date < program.end_date
        and today < program.end_date,
        "can_restore": allowed and withdrawal is not None and today < program.end_date,
        "withdrawal_form": form or WithdrawalForm(initial={"date_from": today}),
    }


@require_POST
def program_withdraw(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    form = WithdrawalForm(request.POST)
    if form.is_valid():
        try:
            services.withdraw(
                request.user,
                program,
                form.cleaned_data["date_from"],
                form.cleaned_data["reason"],
                form.cleaned_data["note"],
            )
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request, "Пациент выбыл: занятия с этой даты убраны, из шахматок он ушёл."
            )
            return redirect("programs:detail", pk=pk)
    messages.error(request, " ".join(form.non_field_errors()) or "Проверьте дату и причину.")
    return redirect("programs:detail", pk=pk)


@require_POST
def program_restore(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    try:
        services.restore(request.user, program)
    except ProgramsError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            "Пациент восстановлен: расписание подобрано заново, в завтрашнюю шахматку он встанет "
            "сам, в сегодняшнюю — через «Не распределены».",
        )
    return redirect("programs:detail", pk=pk)


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
