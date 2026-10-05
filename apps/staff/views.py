"""Экраны «Смены» (FR-STF-4) и «Распорядок инструкторов» (FR-STF-5).

Представления тонкие: права и правила — в services. Действия HTMX возвращают фрагмент целиком
(``hx-swap="outerHTML"``), ошибки форм — в том же фрагменте со статусом 200.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.scheduling import staff_changes

from . import services
from .forms import BlockForm, DutyForm, EndDutyForm, ShiftPatternForm
from .models import Instructor, InstructorBlock, InstructorDuty

MONTHS = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]  # fmt: skip


# --- Смены ---------------------------------------------------------------------------------


def shifts(request: HttpRequest) -> HttpResponse:
    services.require_staff_manager(request.user)
    return render(request, "staff/shifts.html", _shifts_context(request, _month(request)))


@require_POST
def shift_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    instructor = get_object_or_404(Instructor, pk=pk, is_active=True)
    try:
        day = date.fromisoformat(request.POST.get("day", ""))
    except ValueError as error:
        raise Http404("Неверная дата.") from error
    services.toggle_shift_day(request.user, instructor, day)
    change = staff_changes.after_shift_toggle(request.user, instructor, day)
    return _shifts_response(request, (day.year, day.month), rebuild=change)


def shift_pattern(request: HttpRequest, pk: int) -> HttpResponse:
    """Задать или сменить шаблон смены инструктора с даты (форма в строке календаря)."""
    services.require_staff_manager(request.user)
    instructor = get_object_or_404(Instructor, pk=pk, is_active=True)
    month = _month(request)
    if request.method == "POST":
        form = ShiftPatternForm(request.POST, prefix="pattern")
        if form.is_valid():
            try:
                services.change_shift_pattern(request.user, instructor, **form.cleaned_data)
            except (services.StaffError, ValidationError) as error:
                _add_errors(form, error)
            else:
                change = staff_changes.after_pattern(
                    request.user, instructor, form.cleaned_data["valid_from"]
                )
                return _shifts_response(request, month, rebuild=change)
    else:
        current = services.current_pattern(instructor, timezone.localdate())
        form = ShiftPatternForm(
            prefix="pattern",
            initial={
                "pattern": current.pattern if current else "",
                "anchor_date": current.anchor_date if current else timezone.localdate(),
                "valid_from": timezone.localdate(),
            },
        )
    return _shifts_response(request, month, pattern_form=form, pattern_for=instructor)


def _month(request: HttpRequest) -> tuple[int, int]:
    value = request.GET.get("month") or request.POST.get("month") or ""
    try:
        year, month = (int(part) for part in value.split("-"))
        date(year, month, 1)
    except ValueError:
        today = timezone.localdate()
        return today.year, today.month
    return year, month


def _shifts_context(request: HttpRequest, month: tuple[int, int], **extra) -> dict:
    year, number = month
    previous = (year - 1, 12) if number == 1 else (year, number - 1)
    following = (year + 1, 1) if number == 12 else (year, number + 1)
    return {
        "rows": services.shift_month(request.user, year, number),
        "days": services.month_days(year, number),
        "month_value": f"{year}-{number:02d}",
        "month_title": f"{MONTHS[number - 1].capitalize()} {year}",
        "previous_month": f"{previous[0]}-{previous[1]:02d}",
        "next_month": f"{following[0]}-{following[1]:02d}",
        "today": timezone.localdate(),
        **extra,
    }


def _shifts_response(request: HttpRequest, month: tuple[int, int], **extra) -> HttpResponse:
    return render(request, "staff/_shift_calendar.html", _shifts_context(request, month, **extra))


# --- Распорядок ----------------------------------------------------------------------------


def duties(request: HttpRequest) -> HttpResponse:
    services.require_staff_manager(request.user)
    instructors = services.active_instructors()
    selected = None
    pk = request.GET.get("instructor", "")
    if pk.isdigit():
        selected = next((item for item in instructors if item.pk == int(pk)), None)
        if selected is None:
            raise Http404("Инструктор не найден.")
    elif instructors:
        selected = instructors[0]
    context = {"instructors": instructors, "selected": selected}
    if selected is not None:
        context |= _duties_context(request, selected) | _blocks_context(request, selected)
    return render(request, "staff/duties.html", context)


@require_POST
def duty_add(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    instructor = get_object_or_404(Instructor, pk=pk, is_active=True)
    form = DutyForm(request.POST, instance=InstructorDuty(instructor=instructor), prefix="duty")
    change = None
    if form.is_valid() and _run(form, services.save_duty, request.user, form.instance):
        change = staff_changes.after_duty(request.user, form.instance)
        form = None
    return _duties_response(request, instructor, add_form=form, rebuild=change)


def duty_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    duty = get_object_or_404(InstructorDuty, pk=pk, instructor__is_active=True)
    form = DutyForm(request.POST or None, instance=duty, prefix="edit")
    if (
        request.method == "POST"
        and form.is_valid()
        and _run(form, services.save_duty, request.user, duty)
    ):
        change = staff_changes.after_duty(request.user, duty)
        return _duties_response(request, duty.instructor, rebuild=change)
    return _duties_response(request, duty.instructor, edit_form=form, editing=duty)


@require_POST
def duty_end(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    duty = get_object_or_404(InstructorDuty, pk=pk, instructor__is_active=True)
    form = EndDutyForm(request.POST, prefix=f"end{duty.pk}")
    error = ""
    if form.is_valid():
        try:
            services.end_duty(request.user, duty, form.cleaned_data["valid_to"])
        except (services.StaffError, ValidationError) as exc:
            error = _text(exc)
    else:
        error = "Укажите последний день."
    if error:
        duty.refresh_from_db()
    return _duties_response(request, duty.instructor, end_error=error, end_for=duty)


@require_POST
def block_add(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    instructor = get_object_or_404(Instructor, pk=pk, is_active=True)
    form = BlockForm(request.POST, instance=InstructorBlock(instructor=instructor), prefix="block")
    change = None
    if form.is_valid() and _run(form, services.save_block, request.user, form.instance):
        change = staff_changes.after_block(request.user, form.instance)
        form = None
    return _blocks_response(request, instructor, block_form=form, rebuild=change)


@require_POST
def block_delete(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_staff_manager(request.user)
    block = get_object_or_404(InstructorBlock, pk=pk, instructor__is_active=True)
    instructor = block.instructor
    services.delete_block(request.user, block)
    return _blocks_response(request, instructor)


def _duties_context(
    request: HttpRequest,
    instructor: Instructor,
    *,
    add_form: DutyForm | None = None,
    edit_form: DutyForm | None = None,
    editing: InstructorDuty | None = None,
    end_error: str = "",
    end_for: InstructorDuty | None = None,
    rebuild=None,
) -> dict:
    today = timezone.localdate()
    if add_form is None:
        add_form = DutyForm(prefix="duty", initial={"valid_from": today})
    return {
        "selected": instructor,
        "duties": services.duties_of(instructor),
        "add_form": add_form,
        "edit_form": edit_form,
        "editing": editing,
        "end_error": end_error,
        "end_for": end_for,
        "today": today,
        "rebuild": rebuild,
    }


def _blocks_context(
    request: HttpRequest,
    instructor: Instructor,
    *,
    block_form: BlockForm | None = None,
    rebuild=None,
) -> dict:
    today = timezone.localdate()
    if block_form is None:
        block_form = BlockForm(prefix="block", initial={"date": today})
    return {
        "selected": instructor,
        "blocks": services.upcoming_blocks(instructor, today),
        "block_form": block_form,
        "rebuild": rebuild,
    }


def _duties_response(request: HttpRequest, instructor: Instructor, **kwargs) -> HttpResponse:
    return render(request, "staff/_duties.html", _duties_context(request, instructor, **kwargs))


def _blocks_response(request: HttpRequest, instructor: Instructor, **kwargs) -> HttpResponse:
    return render(request, "staff/_blocks.html", _blocks_context(request, instructor, **kwargs))


def _run(form, action, *args) -> bool:
    """Вызывает сервис; понятные ошибки — в форму, а не 500."""
    try:
        action(*args)
    except (services.StaffError, ValidationError) as error:
        _add_errors(form, error)
        return False
    return True


def _add_errors(form, error: Exception) -> None:
    if isinstance(error, ValidationError) and hasattr(error, "error_dict"):
        for field, messages in error.message_dict.items():
            form.add_error(field if field in form.fields else None, messages)
    else:
        form.add_error(None, _text(error))


def _text(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return " ".join(error.messages)
    return str(error)
