"""Экраны «Смены» (FR-STF-4) и «Распорядок инструкторов» (FR-STF-5) — сетки с кликом по ячейке.

Представления тонкие: права и правила — в services. Действия HTMX возвращают фрагмент целиком
(``hx-swap="outerHTML"``), ошибки форм — в том же фрагменте со статусом 200.
"""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.catalog.models import GroupSession, InstructorSlot
from apps.scheduling import board, staff_changes

from . import services
from .forms import ShiftPatternForm, board_sessions
from .models import BlockKind, Instructor

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
    staff_changes.after_shift_toggle(request.user, instructor, day)
    return _shifts_response(request, (day.year, day.month))


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
                staff_changes.after_pattern(
                    request.user, instructor, form.cleaned_data["valid_from"]
                )
                return _shifts_response(request, month)
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
    return render(request, "staff/duties.html", _duties_context(request, _day(request)))


@require_POST
def duty_paint(request: HttpRequest) -> HttpResponse:
    """Клик по ячейке распорядка выбранной «кистью» (FR-STF-5), как клик в «Сменах»."""
    services.require_staff_manager(request.user)
    day = _day(request)
    instructor = get_object_or_404(
        Instructor, pk=_int(request.POST.get("instructor")), is_active=True
    )
    slot = get_object_or_404(InstructorSlot, pk=_int(request.POST.get("slot")))
    kind = request.POST.get("kind", "")
    session = None
    if kind == BlockKind.GROUP_LEAD and request.POST.get("session"):
        session = get_object_or_404(
            GroupSession, pk=_int(request.POST.get("session")), is_active=True
        )
    message, error = "", ""
    try:
        message = board.paint_duty(
            request.user,
            instructor,
            slot,
            day,
            kind,
            once=request.POST.get("mode") == "once",
            label=request.POST.get("label", ""),
            group_session=session,
        )
    except board.BoardError as exc:
        error = str(exc)
    return render(
        request,
        "staff/_duty_grid.html",
        _duties_context(request, day, message=message, error=error),
    )


def _day(request: HttpRequest) -> date:
    value = request.GET.get("day") or request.POST.get("day") or ""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return timezone.localdate()


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def _duties_context(request: HttpRequest, day: date, **extra) -> dict:
    slots, rows = services.duty_grid(request.user, day)
    return {
        "day": day,
        "slots": slots,
        "rows": rows,
        "previous_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "today": timezone.localdate(),
        "kinds": [(kind.value, kind.label) for kind in board.CELL_BLOCK_KINDS],
        "free": board.FREE,
        "sessions": board_sessions(),
        **extra,
    }


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
