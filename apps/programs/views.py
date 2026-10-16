from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from apps.accounts.access import current_department, is_attending_doctor
from apps.exchange.services import can_update_from_sheet
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
    # «-room» — по убыванию; неизвестный столбец — порядок по умолчанию, а не ошибка.
    order = request.GET.get("sort", "")
    descending = order.startswith("-")
    try:
        sort = services.Sort(order.removeprefix("-"))
    except ValueError:
        sort, descending = services.Sort.ROOM, False
    programs = services.programs_for(
        request.user, department, period=period, query=query, sort=sort, descending=descending
    )
    return render(
        request,
        "programs/program_list.html",
        {
            "programs": programs,
            "sort": sort,
            "descending": descending,
            "order": f"{'-' if descending else ''}{sort}",
            "columns": [
                (services.Sort.ROOM, "Палата"),
                (services.Sort.NAME, "ФИО"),
                (services.Sort.SHRM, "ШРМ"),
                (services.Sort.COURSE, "Курс"),
                (services.Sort.DOCTOR, "Лечащий врач"),
                (services.Sort.STATUS, "Состояние"),
            ],
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
        raise PermissionDenied("Нет права создавать программы в этом отделении.")
    program = Program(department=department, start_date=date.today(), source=ProgramSource.MANUAL)
    if is_attending_doctor(request.user, department):
        program.attending_doctor = request.user
    return _program_form(request, program, "Новая программа вручную")


def program_repeat(request: HttpRequest, pk: int) -> HttpResponse:
    """«Повторный курс» со страницы прошлой программы (FR-PRG-10): данные пациента те же,
    курс — новый, по умолчанию назначения берутся из прошлого курса."""
    previous = services.get_program_or_404(request.user, pk)
    if not services.can_create(request.user, previous.department):
        raise PermissionDenied("Нет права создавать программы в этом отделении.")
    if not previous.is_finished(board_days.today()) and previous.withdrawal is None:
        messages.error(request, "Курс ещё идёт — повторный курс создаётся после его окончания.")
        return redirect("programs:detail", pk=pk)
    doctor = previous.attending_doctor
    if not services.doctors_for(previous.department).filter(pk=doctor.pk).exists():
        doctor = request.user if is_attending_doctor(request.user, previous.department) else None
    program = Program(
        department=previous.department,
        full_name=previous.full_name,
        sex=previous.sex,
        age=previous.age,
        room=previous.room,
        shrm=previous.shrm,
        attending_doctor=doctor,
        start_date=board_days.today(),
        source=ProgramSource.MANUAL,
    )
    return _program_form(request, program, "Повторный курс", repeat_of=previous)


def program_edit(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not services.can_edit(request.user, program):
        raise PermissionDenied("Нет права править программы этого отделения.")
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
            "can_delete": services.can_delete(request.user, program),
            "can_update_from_sheet": can_update_from_sheet(request.user, program),
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
            "Пациент восстановлен: расписание подобрано заново, в завтрашнее расписание он встанет "
            "сам, в сегодняшнюю — через «Не распределены».",
        )
    return redirect("programs:detail", pk=pk)


def program_delete(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not services.can_delete(request.user, program):
        raise PermissionDenied("Нет права удалять программы.")
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


# --- Назначения (HTMX) ----------------------------------------------------------------
# Таблица только показывает строки; добавление и правка — в панели справа. Действие в панели
# возвращает панель (цель запроса) и вне цели (hx-swap-oob) — таблицу и расписание.


def prescription_add(request: HttpRequest, pk: int) -> HttpResponse:
    program = services.get_program_or_404(request.user, pk)
    if not _editable(request, program):
        return _panel_response(request, program)
    form = PrescriptionForm(request.POST or None, instance=Prescription(program=program))
    if request.method == "POST" and form.is_valid():
        try:
            services.add_prescription(request.user, form.instance)
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            return _panel_response(request, program, changed=True)
    return _panel_response(request, program, form=form)


def prescription_edit(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    if not _editable(request, program):
        return _panel_response(request, program)
    # Префикс: поля правки не путаются с полями добавления в истории браузера и тестах.
    form = PrescriptionEditForm(request.POST or None, instance=prescription, prefix="edit")
    if request.method == "POST" and form.is_valid():
        try:
            services.update_prescription(request.user, form.instance)
        except ProgramsError as error:
            form.add_error(None, str(error))
        else:
            return _panel_response(request, program, changed=True)
    return _panel_response(request, program, form=form, editing=prescription)


@require_POST
def prescription_delete(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    _run(request, services.delete_prescription, request.user, prescription)
    return _panel_response(request, program, changed=True)


@require_POST
def prescription_cancel(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    """«Отменить с завтра» или «Вернуть на весь курс» — самые частые правки одной кнопкой."""
    program, prescription = _get_prescription(request, pk, prescription_pk)
    day = None if request.POST.get("restore") else board_days.today() + timedelta(days=1)
    try:
        services.cancel_prescription(request.user, prescription, day)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    except ProgramsError as error:
        messages.error(request, str(error))
    prescription.refresh_from_db()
    form = PrescriptionEditForm(instance=prescription, prefix="edit")
    return _panel_response(request, program, form=form, editing=prescription, changed=True)


@require_POST
def prescription_move(
    request: HttpRequest, pk: int, prescription_pk: int, direction: str
) -> HttpResponse:
    program, prescription = _get_prescription(request, pk, prescription_pk)
    step = {"up": -1, "down": 1}.get(direction)
    if step is None:
        raise Http404
    _run(request, services.move_prescription, request.user, prescription, step)
    # Панель остаётся открытой: строку двигают несколько раз подряд.
    prescription.refresh_from_db()
    form = PrescriptionEditForm(instance=prescription, prefix="edit")
    return _panel_response(request, program, form=form, editing=prescription, changed=True)


@require_POST
def prescription_reorder(request: HttpRequest, pk: int) -> HttpResponse:
    """Порядок строк после перетаскивания: id действующих назначений сверху вниз."""
    program = services.get_program_or_404(request.user, pk)
    try:
        order = [int(value) for value in request.POST.getlist("order")]
    except ValueError as error:
        raise Http404 from error
    _run(request, services.reorder_prescriptions, request.user, program, order)
    return _prescriptions_response(request, program)


def _editable(request: HttpRequest, program: Program) -> bool:
    """Чужая роль — 403; выбывший пациент — сообщение вместо панели."""
    try:
        services.require_editable(request.user, program)
    except ProgramsError as error:
        messages.error(request, str(error))
        return False
    return True


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


def _prescriptions_context(request, program, *, selected=None, oob=False) -> dict:
    """Действующие строки — сверху в порядке карты, отменённые (FR-PRG-3) — свёрнуты внизу."""
    today = board_days.today()
    items = list(program.prescriptions.select_related("procedure"))
    return {
        "program": program,
        "active": [item for item in items if not item.is_cancelled(today)],
        "cancelled": [item for item in items if item.is_cancelled(today)],
        "can_edit": services.can_edit(request.user, program),
        "selected": selected,
        "oob": oob,
    }


def _prescriptions_response(request, program) -> HttpResponse:
    """Таблица назначений и — вне основной цели HTMX (hx-swap-oob) — блок расписания:
    изменение назначения сразу пересобирает расписание, экран должен это показать."""
    program.refresh_from_db()
    table = render_to_string(
        "programs/_prescriptions.html", _prescriptions_context(request, program), request=request
    )
    return HttpResponse(table + _schedule_oob(request, program))


def _panel_response(
    request, program, *, form=None, editing=None, changed: bool = False
) -> HttpResponse:
    """Панель (пустая — закрыта) и таблица с выделенной строкой; после изменения —
    ещё и расписание."""
    program.refresh_from_db()
    panel = render_to_string(
        "programs/_prescription_panel.html",
        {
            "program": program,
            "form": form,
            "editing": editing,
            "tomorrow": board_days.today() + timedelta(days=1),
            "today": board_days.today(),
        },
        request=request,
    )
    table = render_to_string(
        "programs/_prescriptions.html",
        _prescriptions_context(
            request, program, selected=editing.pk if editing else None, oob=True
        ),
        request=request,
    )
    return HttpResponse(panel + table + (_schedule_oob(request, program) if changed else ""))


def _schedule_oob(request, program) -> str:
    return render_to_string(
        "scheduling/_schedule.html",
        {
            "program": program,
            "schedule": program_schedule(program),
            "can_schedule": can_schedule(request.user, program),
            "oob": True,
        },
        request=request,
    )
