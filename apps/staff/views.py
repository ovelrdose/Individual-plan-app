"""Раздел «Инструкторы» (решение 87): «Состав и смены» — календарь смен (FR-STF-4) с карточкой
инструктора в окне (FR-STF-1, 2), «Распорядок» (FR-STF-5) и «Загрузка» (FR-SCH-17) — вкладки.

Представления тонкие: права и правила — в services. Действия HTMX возвращают фрагмент целиком
(``hx-swap="outerHTML"``), ошибки форм — в том же фрагменте со статусом 200.
"""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.access import departments_for
from apps.catalog.models import GroupSession, InstructorSlot
from apps.scheduling import board, staff_changes

from . import services
from .forms import InstructorForm, board_sessions
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
    items = list(Instructor.objects.filter(is_active=False).order_by("short_name"))
    return {
        "rows": services.shift_month(request.user, year, number),
        "archived": items,
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


# --- Карточка инструктора (окно на «Составе и сменах») -------------------------------------
# Действие возвращает содержимое окна, календарь — через hx-swap-oob (как «Справочники»).


def _instructor(request: HttpRequest, pk: int) -> Instructor:
    services.require_instructors(request.user)
    return get_object_or_404(Instructor, pk=pk)


def _pattern_initial(instructor: Instructor | None) -> dict:
    today = timezone.localdate()
    # У архивного шаблона в окне нет: смену ему задают после возврата.
    current = (
        services.current_pattern(instructor, today) if instructor and instructor.is_active else None
    )
    return {
        "pattern": current.pattern if current else "",
        "anchor_date": current.anchor_date if current else None,
        "valid_from": today,
    }


def instructor_add(request: HttpRequest) -> HttpResponse:
    services.require_instructors(request.user)
    form = InstructorForm(request.POST or None, prefix="ins", initial=_pattern_initial(None))
    if request.method == "POST" and form.is_valid():
        saved, error = _save_instructor(request, form)
        if saved is not None:
            message = f"Инструктор «{saved.short_name}» заведён."
            if not form.cleaned_data["pattern"]:
                message += " Шаблон смены не задан — пока он не работает ни в один день."
            return _instructor_panel(request, saved, message=message, error=error)
    return _instructor_panel(request, None, form=form)


def instructor_edit(request: HttpRequest, pk: int) -> HttpResponse:
    instructor = _instructor(request, pk)
    if request.method != "POST":
        return _instructor_panel(request, instructor)
    form = InstructorForm(
        request.POST, instance=instructor, prefix="ins", initial=_pattern_initial(instructor)
    )
    if form.is_valid():
        saved, error = _save_instructor(request, form)
        if saved is not None:
            return _instructor_panel(
                request, saved, message="" if error else "Сохранено.", error=error
            )
    instructor.refresh_from_db()
    return _instructor_panel(request, instructor, form=form)


def _save_instructor(request: HttpRequest, form: InstructorForm) -> tuple[Instructor | None, str]:
    """Карточка, затем шаблон смены, если его сменили. Ошибка карточки — в форме (None);
    ошибка шаблона — текстом: карточка к этому времени уже сохранена."""
    try:
        saved = services.save_instructor_card(
            request.user, form.instance, form.cleaned_data["partner"]
        )
    except (services.StaffError, ValidationError) as error:
        _add_errors(form, error)
        return None, ""
    change = form.pattern_change()
    if change is None:
        return saved, ""
    try:
        services.change_shift_pattern(request.user, saved, **change)
    except (services.StaffError, ValidationError) as error:
        return saved, f"Карточка сохранена, а шаблон смены — нет: {_text(error)}"
    staff_changes.after_pattern(request.user, saved, change["valid_from"])
    return saved, ""


@require_POST
def instructor_delete(request: HttpRequest, pk: int) -> HttpResponse:
    instructor = _instructor(request, pk)
    if not request.POST.get("confirm"):
        details = []
        if instructor.partner_id:
            details.append(f"Пара 2/2 разорвётся: {instructor.partner} останется один.")
        confirm = {
            "used": staff_changes.instructor_is_used(instructor),
            "programs": None,
            "details": details,
            "url": reverse("staff:instructor_delete", args=[instructor.pk]),
        }
        return _instructor_panel(request, instructor, confirm=confirm)
    try:
        removed = staff_changes.delete_instructor(request.user, instructor)
    except services.StaffError as error:
        return _instructor_panel(request, instructor, error=str(error))
    if not removed.archived:
        return _instructor_panel(
            request, None, message=f"Инструктор «{instructor.short_name}» удалён.", closed=True
        )
    instructor.refresh_from_db()
    return _instructor_panel(request, instructor, removed=removed)


@require_POST
def instructor_restore(request: HttpRequest, pk: int) -> HttpResponse:
    instructor = _instructor(request, pk)
    staff_changes.restore_instructor(request.user, instructor)
    instructor.refresh_from_db()
    return _instructor_panel(
        request,
        instructor,
        message=f"«{instructor.short_name}» снова работает. Задайте ему шаблон смены и напарника.",
    )


def _instructor_panel(
    request: HttpRequest,
    instructor: Instructor | None,
    *,
    form: InstructorForm | None = None,
    message: str = "",
    error: str = "",
    confirm: dict | None = None,
    removed: object | None = None,
    closed: bool = False,
) -> HttpResponse:
    if instructor is not None and form is None:
        form = InstructorForm(
            instance=instructor, prefix="ins", initial=_pattern_initial(instructor)
        )
    displaced, hidden = [], 0
    if removed is not None:
        # Фамилии — только пациентов своих отделений (FR-ACC-2), чужие — числом.
        own = set(departments_for(request.user).values_list("pk", flat=True))
        displaced = [d for d in removed.displaced if d.program.department_id in own]
        hidden = len({d.program.pk for d in removed.displaced} - {d.program.pk for d in displaced})
    month = _month(request)
    panel = render_to_string(
        "staff/_instructor_panel.html",
        {
            "instructor": instructor,
            "form": None if closed else form,
            "patterns": list(instructor.shift_patterns.order_by("valid_from"))
            if instructor is not None and instructor.pk
            else [],
            "month_value": f"{month[0]}-{month[1]:02d}",
            "message": message,
            "error": error,
            "confirm": confirm,
            "removed": removed,
            "displaced": displaced,
            "hidden": hidden,
        },
        request=request,
    )
    calendar = render_to_string(
        "staff/_shift_calendar.html",
        _shifts_context(request, month, selected=instructor.pk if instructor else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + calendar)


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
