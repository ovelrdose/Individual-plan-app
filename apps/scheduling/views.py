import ipaddress
from dataclasses import dataclass
from datetime import date, time
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.cards.views import XLSX
from apps.cards.xlsx.board import render_board
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure
from apps.programs.models import Prescription, Program
from apps.programs.services import get_program_or_404
from apps.staff.models import Instructor
from apps.staff.services import can_manage_staff

from . import board, manual, reports, services, staff_changes
from .domain import is_weekend
from .forms import EquipmentForm, GroupSessionForm
from .models import Booking, BookingKind, StaffChange


@require_POST
def program_replan(request: HttpRequest, pk: int) -> HttpResponse:
    """«Подобрать заново» — например, после изменения расписания групп (специалист ФР)."""
    program = get_program_or_404(request.user, pk)
    proposal = services.replan_by(request.user, program)
    if proposal.conflicts:
        messages.warning(request, "Расписание подобрано, но есть конфликты — см. ниже.")
    else:
        messages.success(request, "Расписание подобрано заново.")
    return redirect("programs:detail", pk=pk)


@require_POST
def program_pool_group(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    """Выбор группы бассейна (HTMX): возвращает блок расписания целиком."""
    program = get_program_or_404(request.user, pk)
    prescription = get_object_or_404(Prescription, pk=prescription_pk, program=program)
    error = ""
    try:
        group = _choice(request, program, "group", Procedure, "Выберите группу бассейна из списка.")
        services.choose_pool_group(request.user, prescription, group)
    except services.ScheduleError as exc:
        error = str(exc)
    return _schedule_response(request, program, error, pool_error=error)


@require_POST
def program_preferred_instructor(request: HttpRequest, pk: int) -> HttpResponse:
    """Инструктор по желанию пациента (HTMX, решение 44): возвращает блок расписания."""
    program = get_program_or_404(request.user, pk)
    error = ""
    try:
        instructor = _choice(
            request, program, "instructor", Instructor, "Выберите инструктора из списка."
        )
        services.choose_preferred_instructor(request.user, program, instructor)
    except services.ScheduleError as exc:
        error = str(exc)
    return _schedule_response(request, program, error, preferred_error=error)


def _choice[T: (Procedure, Instructor)](
    request: HttpRequest, program: Program, field: str, model: type[T], message: str
) -> T | None:
    """Значение из списка выбора: пусто — снять выбор; неверное значение — ошибка, выбор не
    меняется (а не молчаливое снятие или 500 на «²», для которой isdigit() истинно)."""
    value = request.POST.get(field, "").strip()
    if not value:
        return None
    try:
        pk = int(value)
        if not 0 < pk < 2**63:  # иначе база ответит ошибкой переполнения
            raise ValueError(value)
        return model.objects.get(pk=pk)
    except (ValueError, model.DoesNotExist):
        # Права важнее текста ошибки: врачу — 403, как и при верном значении.
        if not services.can_schedule(request.user, program):
            raise PermissionDenied("Расписание правит специалист ФР.") from None
        raise services.ScheduleError(message) from None


def _schedule_response(
    request: HttpRequest, program: Program, error: str, **extra: str
) -> HttpResponse:
    if not request.headers.get("HX-Request"):
        # Без JavaScript форма уходит обычным POST — возвращаем на страницу программы.
        if error:
            messages.error(request, error)
        return redirect("programs:detail", pk=program.pk)
    program.refresh_from_db()
    return render(
        request,
        "scheduling/_schedule.html",
        {
            "program": program,
            "schedule": services.program_schedule(program),
            "can_schedule": services.can_schedule(request.user, program),
            **extra,
        },
    )


# --- Экран «Расписание групп» (FR-SCH-18). Действия HTMX возвращают фрагмент целиком. ---------


def group_schedule(request: HttpRequest) -> HttpResponse:
    services.require_group_schedule(request.user)
    return render(
        request,
        "scheduling/group_schedule.html",
        {**_sessions_context(request), **_equipment_context()},
    )


@require_POST
def session_add(request: HttpRequest) -> HttpResponse:
    services.require_group_schedule(request.user)
    form = GroupSessionForm(request.POST, user=request.user)
    result = None
    if form.is_valid():
        result = _save(form, services.save_group_session, request.user)
        if result is not None:
            form = None
    return _sessions_response(request, add_form=form, result=result)


def session_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    session = get_object_or_404(
        GroupSession.objects.filter(procedure__in=services.group_schedule_procedures(request.user)),
        pk=pk,
    )
    form = GroupSessionForm(
        request.POST or None, instance=session, user=request.user, prefix="edit"
    )
    if request.method == "POST" and form.is_valid():
        result = _save(form, services.save_group_session, request.user)
        if result is not None:
            return _sessions_response(request, result=result)
    return _sessions_response(request, edit_form=form, editing=session)


def equipment_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    equipment = get_object_or_404(Equipment, pk=pk)
    form = EquipmentForm(request.POST or None, instance=equipment, prefix="eq")
    if request.method == "POST" and form.is_valid():
        result = _save(form, services.save_equipment, request.user)
        if result is not None:
            return _equipment_response(request, result=result)
    return _equipment_response(request, edit_form=form, editing=equipment)


def _save(form, action, user):
    try:
        return action(user, form.instance)
    except services.ScheduleError as error:
        form.add_error(None, str(error))
        return None
    except ValidationError as error:
        # Проверки модели, которых нет в форме (например, окно не вмещает ни одной записи).
        for field, messages_ in error.message_dict.items():
            form.add_error(field if field in form.fields else None, messages_)
        return None


def _sessions_context(request, *, add_form=None, edit_form=None, editing=None, result=None):
    if add_form is None:
        add_form = GroupSessionForm(user=request.user, initial={"duration_min": 30})
    return {
        "sessions": services.group_sessions(request.user),
        "add_form": add_form,
        "edit_form": edit_form,
        "editing": editing,
        "sessions_result": result,
    }


def _equipment_context(*, edit_form=None, editing=None, result=None):
    return {
        "equipment_list": Equipment.objects.order_by("name"),
        "equipment_form": edit_form,
        "equipment_editing": editing,
        "equipment_result": result,
    }


def _sessions_response(request, **kwargs) -> HttpResponse:
    return render(request, "scheduling/_group_sessions.html", _sessions_context(request, **kwargs))


def _equipment_response(request, **kwargs) -> HttpResponse:
    return render(request, "scheduling/_equipment.html", _equipment_context(**kwargs))


# --- Шахматка на дату (FR-SCH-14…16) ------------------------------------------------------
# Ячейку открывает специалист ФР: панель ячейки грузится HTMX-запросом; действие возвращает
# панель с итогом и шахматку целиком (hx-swap-oob), ошибка — панель с текстом, статус 200.


def board_view(request: HttpRequest) -> HttpResponse:
    day = _board_day(request.GET.get("date"))
    return render(request, "scheduling/board.html", {"board": board.board(request.user, day)})


def board_cell(request: HttpRequest) -> HttpResponse:
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Шахматку правит специалист ФР.")
    cell = _cell(request, request.GET)
    return _panel(request, cell)


@require_POST
def board_place(request: HttpRequest) -> HttpResponse:
    cell = _cell(request, request.POST)
    program = get_object_or_404(Program, pk=_int(request.POST.get("program")))
    return _act(
        request,
        cell,
        lambda reason: board.place_patient(
            request.user, program, cell.instructor, cell.slot, cell.day, reason=reason
        ),
        "Пациент поставлен.",
    )


@require_POST
def board_move(request: HttpRequest, pk: int) -> HttpResponse:
    booking = get_object_or_404(Booking, pk=pk, kind=BookingKind.INDIVIDUAL)
    cell = _cell(request, request.POST)
    instructor_pk, _sep, slot_pk = request.POST.get("target", "").partition(":")
    instructor = get_object_or_404(Instructor, pk=_int(instructor_pk))
    slot = get_object_or_404(InstructorSlot, pk=_int(slot_pk))
    version = _int(request.POST.get("version"))
    return _act(
        request,
        cell,
        lambda reason: board.move_patient(
            request.user, booking, version, instructor, slot, reason=reason
        ),
        "Пациент перенесён.",
    )


@require_POST
def board_remove(request: HttpRequest, pk: int) -> HttpResponse:
    booking = get_object_or_404(Booking, pk=pk, kind=BookingKind.INDIVIDUAL)
    cell = _cell(request, request.POST)
    version = _int(request.POST.get("version"))
    return _act(
        request,
        cell,
        lambda _reason: board.remove_patient(request.user, booking, version),
        "Занятие на эту дату убрано — подбор его не вернёт.",
    )


@require_POST
def board_block(request: HttpRequest) -> HttpResponse:
    cell = _cell(request, request.POST)
    until = request.POST.get("until", "").strip()
    try:
        until_day = date.fromisoformat(until) if until else None
    except ValueError:
        return _panel(request, cell, error="Неверная дата «по дату».")

    def action(_reason: str) -> None:
        result = board.add_block(
            request.user,
            cell.instructor,
            cell.slot,
            cell.day,
            request.POST.get("kind", ""),
            request.POST.get("label", ""),
            until_day,
        )
        if result.skipped:
            days = ", ".join(f"{day:%d.%m}" for day in result.skipped)
            raise _Done(f"Блок поставлен. Пропущены даты с пациентом в ячейке: {days}.")

    return _act(request, cell, action, "Блок поставлен.")


@require_POST
def board_block_clear(request: HttpRequest) -> HttpResponse:
    cell = _cell(request, request.POST)
    return _act(
        request,
        cell,
        lambda _reason: board.clear_block(request.user, cell.instructor, cell.slot, cell.day),
        "Блок убран.",
    )


class _Done(Exception):
    """Действие выполнено, но итог нужно показать особым текстом."""


@dataclass(frozen=True)
class _Cell:
    day: date
    instructor: Instructor
    slot: InstructorSlot


def _board_day(value: str | None) -> date:
    try:
        return date.fromisoformat(value) if value else timezone.localdate()
    except ValueError:
        return timezone.localdate()


def _int(value: str | None) -> int:
    """Номер из формы; мусор — 404, а не 500 (как у выбора инструктора)."""
    try:
        number = int((value or "").strip())
    except ValueError:
        raise Http404("Неверный номер.") from None
    if not 0 < number < 2**63:
        raise Http404("Неверный номер.")
    return number


def _cell(request: HttpRequest, data) -> _Cell:
    try:
        day = date.fromisoformat(data.get("date", ""))
    except ValueError:
        raise Http404("Неверная дата.") from None
    instructor = get_object_or_404(Instructor, pk=_int(data.get("instructor")), is_active=True)
    slot = get_object_or_404(InstructorSlot, pk=_int(data.get("slot")))
    return _Cell(day, instructor, slot)


def _act(request: HttpRequest, cell: _Cell, action, done: str) -> HttpResponse:
    reason = request.POST.get("reason", "")
    try:
        action(reason)
    except _Done as result:
        done = str(result)
    except board.ConfirmationRequired as error:
        return _panel(request, cell, confirm=error.violations)
    except board.BoardError as error:
        return _panel(request, cell, error=str(error))
    return render(
        request,
        "scheduling/_board_done.html",
        {"done": done, "board": board.board(request.user, cell.day)},
    )


def _panel(
    request: HttpRequest, cell: _Cell, *, error: str = "", confirm: list | None = None
) -> HttpResponse:
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Шахматку правит специалист ФР.")
    current = board.board(request.user, cell.day)
    seat = next(
        (
            s
            for column in current.columns
            for c in column.cells
            if c.slot.pk == cell.slot.pk
            for s in c.seats
            if s.instructor.pk == cell.instructor.pk
        ),
        None,
    )
    booking = seat.booking if seat else None
    editable = booking is None or board.can_edit_patient(request.user, booking.program)
    programs = (
        board.programs_for_cell(request.user, cell.day)
        if seat and seat.is_free and not current.is_weekend
        else []
    )
    targets = [
        (
            f"{instructor.pk}:{slot.pk}",
            f"{slot.start:%H:%M} — {instructor.short_name}"
            + (" (вечер)" if slot.is_evening else ""),
        )
        for instructor, slot in (
            board.free_seats(cell.day, booking) if booking and editable else []
        )
    ]
    return render(
        request,
        "scheduling/_board_cell.html",
        {
            "cell": cell,
            "seat": seat,
            "weekend": current.is_weekend,
            "editable": editable,
            "programs": programs,
            "many_departments": len({p.department_id for p in programs}) > 1,
            "targets": targets,
            "block_kinds": [(k.value, k.label) for k in board.CELL_BLOCK_KINDS],
            "error": error,
            "confirm": confirm or [],
            "posted": request.POST,
        },
    )


@require_POST
def board_prefer(request: HttpRequest, pk: int) -> HttpResponse:
    """«Закрепить за этим инструктором» из ячейки с пациентом (FR-SCH-15)."""
    booking = get_object_or_404(Booking, pk=pk, kind=BookingKind.INDIVIDUAL)
    cell = _cell(request, request.POST)
    return _act(
        request,
        cell,
        lambda _reason: board.prefer_instructor(request.user, booking),
        f"{booking.instructor.short_name if booking.instructor else 'Инструктор'} — "
        "инструктор по желанию пациента, индивидуальные пересобраны.",
    )


# --- Ручная правка на странице программы (FR-SCH-9/10) -----------------------------------
# Панель занятия грузится HTMX-запросом; действие возвращает панель с итогом и блок
# расписания целиком (hx-swap-oob), ошибка — панель с текстом, статус 200.


def booking_panel(request: HttpRequest, pk: int) -> HttpResponse:
    booking = _program_booking(request, pk)
    return _booking_panel(request, booking)


@require_POST
def booking_edit(request: HttpRequest, pk: int) -> HttpResponse:
    booking = _program_booking(request, pk)
    target = _target(booking, request.POST)
    return _booking_act(
        request,
        booking,
        lambda reason: manual.edit_booking(
            request.user,
            booking,
            _int(request.POST.get("version")),
            target,
            scope=request.POST.get("scope", manual.ONE),
            reason=reason,
        ),
        "Занятие изменено и закреплено.",
    )


@require_POST
def booking_remove(request: HttpRequest, pk: int) -> HttpResponse:
    booking = _program_booking(request, pk)
    return _booking_act(
        request,
        booking,
        lambda _reason: manual.remove_booking(
            request.user,
            booking,
            _int(request.POST.get("version")),
            scope=request.POST.get("scope", manual.ONE),
        ),
        "Занятие убрано — подбор его не вернёт.",
    )


@require_POST
def booking_unpin(request: HttpRequest, pk: int) -> HttpResponse:
    booking = _program_booking(request, pk)
    return _booking_act(
        request,
        booking,
        lambda _reason: manual.unpin(
            request.user,
            booking,
            _int(request.POST.get("version")),
            scope=request.POST.get("scope", manual.ONE),
        ),
        "Занятие откреплено — подбор снова распоряжается им.",
    )


@require_POST
def booking_restore(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    """«Вернуть» убранное занятие на дату (решение 57)."""
    program = get_program_or_404(request.user, pk)
    prescription = get_object_or_404(Prescription, pk=prescription_pk, program=program)
    try:
        day = date.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        raise Http404("Неверная дата.") from None
    try:
        manual.restore_date(request.user, prescription, day)
    except manual.EditError as error:
        if not manual.can_edit(request.user, program):
            raise PermissionDenied(str(error)) from None
        return _booking_done(request, program, "", error=str(error))
    return _booking_done(request, program, "Занятие возвращено — подбор поставил его снова.")


def _program_booking(request: HttpRequest, pk: int) -> Booking:
    """Занятие программы, которую пользователь видит; чужое — 404 (FR-ACC-2)."""
    booking = get_object_or_404(
        Booking.objects.select_related(
            "program", "procedure", "instructor__partner", "slot", "equipment", "group_session"
        ),
        pk=pk,
    )
    get_program_or_404(request.user, booking.program_id)
    return booking


def _target(booking: Booking, data) -> manual.Target:
    if booking.kind == BookingKind.INDIVIDUAL:
        instructor_pk, _sep, slot_pk = data.get("target", "").partition(":")
        slot = get_object_or_404(InstructorSlot, pk=_int(slot_pk))
        instructor = (
            get_object_or_404(Instructor, pk=_int(instructor_pk), is_active=True)
            if instructor_pk
            else None
        )
        return manual.Target(instructor=instructor, slot=slot)
    if booking.kind in (BookingKind.LFK_GROUP, BookingKind.POOL):
        session = get_object_or_404(GroupSession, pk=_int(data.get("session")), is_active=True)
        return manual.Target(session=session)
    try:
        start = time.fromisoformat(data.get("start", ""))
    except ValueError:
        raise Http404("Неверное время.") from None
    return manual.Target(start=start)


def _booking_act(request: HttpRequest, booking: Booking, action, done: str) -> HttpResponse:
    reason = request.POST.get("reason", "")
    try:
        result = action(reason)
    except manual.ConfirmationRequired as error:
        return _booking_panel(request, booking, confirm=error.violations)
    except manual.EditError as error:
        if not manual.can_edit(request.user, booking.program):
            raise PermissionDenied(str(error)) from None
        return _booking_panel(request, booking, error=str(error))
    skipped = getattr(result, "skipped", [])
    return _booking_done(request, booking.program, done, skipped=skipped)


def _booking_done(
    request: HttpRequest, program: Program, done: str, *, error: str = "", skipped=()
) -> HttpResponse:
    program.refresh_from_db()
    return render(
        request,
        "scheduling/_booking_done.html",
        {
            "done": done,
            "error": error,
            "skipped": skipped,
            "program": program,
            "schedule": services.program_schedule(program),
            "can_schedule": services.can_schedule(request.user, program),
        },
    )


def _booking_panel(
    request: HttpRequest, booking: Booking, *, error: str = "", confirm: list | None = None
) -> HttpResponse:
    if not manual.can_edit(request.user, booking.program):
        raise PermissionDenied("Расписание правит специалист ФР.")
    current = (
        Booking.objects.filter(pk=booking.pk)
        .select_related("procedure", "instructor", "slot", "equipment", "group_session")
        .first()
    )
    if current is None:
        return _booking_done(
            request, booking.program, "", error="Занятие уже изменено — обновите страницу."
        )
    weekend = is_weekend(current.date)
    options: list[tuple[str, str]] = []
    selected = ""
    if current.kind == BookingKind.INDIVIDUAL:
        if weekend:
            options = [
                (f":{slot.pk}", f"{slot.start:%H:%M}" + (" (вечер)" if slot.is_evening else ""))
                for slot in InstructorSlot.objects.order_by("start")
            ]
            selected = f":{current.slot_id}"
        else:
            options = [
                (
                    f"{current.instructor_id}:{current.slot_id}",
                    f"{current.start:%H:%M} — {current.instructor.short_name} (сейчас)",
                )
            ] + [
                (
                    f"{instructor.pk}:{slot.pk}",
                    f"{slot.start:%H:%M} — {instructor.short_name}"
                    + (" (вечер)" if slot.is_evening else ""),
                )
                for instructor, slot in board.free_seats(current.date, current)
            ]
            selected = options[0][0]
    elif current.kind in (BookingKind.LFK_GROUP, BookingKind.POOL):
        options = [
            (str(session.pk), f"{session.start_time:%H:%M} {session.effective_place}".strip())
            for session in current.procedure.sessions.filter(is_active=True).order_by("start_time")
        ]
        selected = str(current.group_session_id or "")
    elif current.kind == BookingKind.EQUIPMENT and current.equipment is not None:
        starts = sorted(set(current.equipment.start_times()) | {current.start})
        options = [(f"{start:%H:%M}", f"{start:%H:%M}") for start in starts]
        selected = f"{current.start:%H:%M}"
    return render(
        request,
        "scheduling/_booking_panel.html",
        {
            "booking": current,
            "program": booking.program,
            "weekend": weekend,
            "options": options,
            "selected": request.POST.get("target")
            or request.POST.get("session")
            or request.POST.get("start")
            or selected,
            "field": {
                BookingKind.INDIVIDUAL: "target",
                BookingKind.LFK_GROUP: "session",
                BookingKind.POOL: "session",
                BookingKind.EQUIPMENT: "start",
            }.get(current.kind, "target"),
            "error": error,
            "confirm": confirm or [],
            "posted": request.POST,
        },
    )


# --- Перестройка при изменении смены (FR-SCH-13, FR-STF-6) ---------------------------------


@require_POST
def board_off(request: HttpRequest) -> HttpResponse:
    """«Не работает» из шахматки одним действием: исключение смены и перестройка (FR-STF-6)."""
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Шахматку правит специалист ФР.")
    instructor = get_object_or_404(Instructor, pk=_int(request.POST.get("instructor")))
    day = _board_day(request.POST.get("date"))
    change = staff_changes.set_not_working(request.user, instructor, day)
    if change is None:
        messages.success(request, f"{instructor.short_name}: {day:%d.%m} не работает.")
        return redirect(f"{reverse('scheduling:board')}?date={day:%Y-%m-%d}")
    return redirect("scheduling:change", pk=change.pk)


def changes(request: HttpRequest) -> HttpResponse:
    """Последние перестройки — чтобы найти сводку и после ухода со страницы."""
    _require_staff(request)
    items = StaffChange.objects.select_related("instructor", "created_by")[:50]
    return render(request, "scheduling/changes.html", {"changes": items})


def change(request: HttpRequest, pk: int) -> HttpResponse:
    _require_staff(request)
    item = get_object_or_404(StaffChange.objects.select_related("instructor", "created_by"), pk=pk)
    problems: list[str] = []
    if request.method == "POST":
        try:
            problems = staff_changes.revert(request.user, item)
        except manual.EditError as error:
            messages.error(request, str(error))
        else:
            if problems:
                messages.warning(request, "Вернули не всё — см. список ниже.")
            else:
                messages.success(request, "Занятия возвращены на прежние места.")
        item.refresh_from_db()
    return render(request, "scheduling/change.html", {"change": item, "problems": problems})


def _require_staff(request: HttpRequest) -> None:
    if not can_manage_staff(request.user):
        raise PermissionDenied("Сводки перестроек смотрит специалист ФР.")


def load(request: HttpRequest) -> HttpResponse:
    """Отчёт «Загрузка инструкторов» за период (FR-SCH-17); по умолчанию — текущий месяц."""
    today = timezone.localdate()
    start = (
        _board_day(request.GET.get("start")) if request.GET.get("start") else today.replace(day=1)
    )
    end = _board_day(request.GET.get("end")) if request.GET.get("end") else today
    report = reports.instructor_load(request.user, start, end)
    return render(request, "scheduling/load.html", {"report": report})


def board_export(request: HttpRequest) -> HttpResponse:
    """Шахматка на дату в .xlsx для печати из MS Excel (FR-CRD-7)."""
    day = _board_day(request.GET.get("date"))
    content = render_board(board.sheet(board.board(request.user, day)))
    response = HttpResponse(content, content_type=XLSX)
    filename = f"Шахматка {day:%d.%m.%Y}.xlsx"
    response["Content-Disposition"] = (
        f"attachment; filename=\"board.xlsx\"; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_not_required
def public_board(request: HttpRequest) -> HttpResponse:
    """Публичная шахматка (FR-ACC-5): без входа, только чтение, только из сетей клиники."""
    if not _public_allowed(request):
        raise Http404("Страница не найдена.")
    day = _board_day(request.GET.get("date"))
    return render(request, "scheduling/public_board.html", {"board": board.board(None, day)})


def _public_allowed(request: HttpRequest) -> bool:
    networks = [item.strip() for item in settings.PUBLIC_BOARD_NETWORKS if item.strip()]
    if not networks:
        return True  # локальная разработка: настройка не задана
    try:
        address = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
        return any(address in ipaddress.ip_network(net, strict=False) for net in networks)
    except ValueError:
        return False
