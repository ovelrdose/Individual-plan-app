import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.cards.views import XLSX
from apps.cards.xlsx.board import render_board
from apps.catalog.models import (
    Equipment,
    GroupSession,
    InstructorSlot,
    Procedure,
    Section,
)
from apps.programs.models import Prescription, Program
from apps.programs.services import get_program_or_404
from apps.programs.views import prescriptions_oob
from apps.staff.models import Instructor

from . import board, board_days, manual, reports, services, staff_changes
from .forms import (
    EquipmentForm,
    GroupForm,
    GroupSessionForm,
    NewEquipmentForm,
    SectionForm,
)
from .models import GROUP_BOOKING_KINDS, Booking, BookingKind


@require_POST
def program_pool_group(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    """Выбор группы бассейна (HTMX): возвращает блок расписания целиком."""
    program = get_program_or_404(request.user, pk)
    prescription = get_object_or_404(Prescription, pk=prescription_pk, program=program)
    error = ""
    with services.watch_displaced(program) as displaced:
        try:
            group = _choice(
                request, program, "group", Procedure, "Выберите группу бассейна из списка."
            )
            services.choose_pool_group(request.user, prescription, group)
        except services.ScheduleError as exc:
            error = str(exc)
    return _schedule_response(request, program, error, pool_error=error, displaced=displaced)


@require_POST
def program_group_time(request: HttpRequest, pk: int, prescription_pk: int) -> HttpResponse:
    """Время группы на весь курс (HTMX, решение 81): возвращает блок расписания целиком —
    с ошибкой или со списком индивидуальных, ушедших в «Не распределены»."""
    program = get_program_or_404(request.user, pk)
    prescription = get_object_or_404(Prescription, pk=prescription_pk, program=program)
    error, displaced = "", []
    try:
        session = _choice(
            request, program, "session", GroupSession, "Выберите время из списка.",
            allowed=services.can_choose_group_time,
        )  # fmt: skip
        result = services.choose_group_time(request.user, prescription, session)
        displaced = result.displaced
    except services.ScheduleError as exc:
        error = str(exc)
    return _schedule_response(request, program, error, group_time_error=error, displaced=displaced)


def _choice[T: Procedure | GroupSession](
    request: HttpRequest,
    program: Program,
    field: str,
    model: type[T],
    message: str,
    *,
    allowed: Callable[[Any, Program], bool] = services.can_schedule,
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
        if not allowed(request.user, program):
            raise PermissionDenied("Нет права править расписание пациентов.") from None
        raise services.ScheduleError(message) from None


def _schedule_response(
    request: HttpRequest, program: Program, error: str, **extra: object
) -> HttpResponse:
    if not request.headers.get("HX-Request"):
        # Без JavaScript форма уходит обычным POST — возвращаем на страницу программы.
        if error:
            messages.error(request, error)
        for item in extra.get("displaced") or []:
            messages.warning(request, str(item))
        return redirect("programs:detail", pk=program.pk)
    program.refresh_from_db()
    schedule = render_to_string(
        "scheduling/_schedule.html",
        {
            "program": program,
            "schedule": services.program_schedule(program),
            "can_schedule": services.can_schedule(request.user, program),
            "can_group_time": services.can_choose_group_time(request.user, program),
            **extra,
        },
        request=request,
    )
    # Строка назначения тоже меняется («в 11:15», сброс времени при смене группы бассейна).
    return HttpResponse(schedule + prescriptions_oob(request, program))


# --- Экран «Расписание групп» (FR-SCH-18). Действия HTMX возвращают фрагмент целиком. ---------


def group_schedule(request: HttpRequest) -> HttpResponse:
    services.require_group_schedule(request.user)
    # Вкладки — разделы групп и последней тренажёры (решение 82): таблицы длинные и правятся
    # по отдельности.
    if request.GET.get("tab") == "equipment":
        return _page(request, equipment=True)
    return _page(request, section=_section(request.GET.get("section")))


def _page(
    request: HttpRequest,
    *,
    section: Section | None = None,
    equipment: bool = False,
    status: int = 200,
    **extra: Any,
) -> HttpResponse:
    sections = services.sections_with_counts(request.user)
    if not equipment and section is None and sections:
        section = sections[0]
    context = _equipment_context() if equipment else _sessions_context(request, section)
    return render(
        request,
        "scheduling/group_schedule.html",
        {
            **context,
            **extra,
            "tab": "equipment" if equipment else "groups",
            "sections": sections,
            "section": section,
            "equipment_count": Equipment.objects.filter(is_active=True).count(),
        },
        status=status,
    )


def _section(value: object) -> Section | None:
    """Раздел из запроса; неизвестный или пустой — первый по порядку."""
    text = str(value or "").strip()
    section = (
        Section.objects.filter(pk=int(text)).first() if text.isdigit() and len(text) < 19 else None
    )
    return section or Section.objects.order_by("order", "name").first()


def section_add(request: HttpRequest) -> HttpResponse:
    return _section_form(request, Section())


def section_edit(request: HttpRequest, pk: int) -> HttpResponse:
    return _section_form(request, get_object_or_404(Section, pk=pk))


def _section_form(request: HttpRequest, section: Section) -> HttpResponse:
    """Раздел — обычная форма (hx-boost): после сохранения меняются вкладки, поэтому
    возвращается страница целиком."""
    services.require_group_schedule(request.user)
    form = SectionForm(request.POST or None, instance=section)
    saved = (
        request.method == "POST"
        and form.is_valid()
        and _save(form, services.save_section, request.user) is not None
    )
    if saved:
        messages.success(request, f"Раздел «{section.name}» сохранён.")
        return redirect(_section_url(section))
    current = section if section.pk else _section(None)
    return _page(request, section=current, section_form=form, section_editing=section)


@require_POST
def section_delete(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    section = get_object_or_404(Section, pk=pk)
    try:
        services.delete_section(request.user, section)
    except services.ScheduleError as error:
        messages.error(request, str(error))
        return redirect(_section_url(section))
    messages.success(request, f"Раздел «{section.name}» удалён.")
    return redirect("scheduling:group_schedule")


def _section_url(section: Section) -> str:
    return f"{reverse('scheduling:group_schedule')}?section={section.pk}"


# Группа и тренажёр правятся в панели справа, как назначения на странице программы: действие
# возвращает панель и — вне основной цели (hx-swap-oob) — таблицу раздела или тренажёров.


def group_add(request: HttpRequest, section_pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    section = get_object_or_404(Section, pk=section_pk)
    form = GroupForm(
        request.POST or None,
        user=request.user,
        prefix="group",
        initial={"section": section, "default_duration_min": 30},
    )
    if request.method == "POST" and form.is_valid():
        saved = _save(
            form,
            lambda user, group: services.create_group(user, group, form.cleaned_data["times"]),
            request.user,
        )
        if saved is not None:
            return _group_panel(
                request, saved.group, message=f"Группа «{saved.group.name}» заведена."
            )
    return _group_panel(request, None, section=section, group_form=form)


def _visible_group(request: HttpRequest, pk: int) -> Procedure:
    return get_object_or_404(
        services.schedule_groups(request.user).select_related("section"), pk=pk
    )


def group_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    group = _visible_group(request, pk)
    if request.method != "POST":
        return _group_panel(request, group)
    section = group.section
    form = GroupForm(request.POST, instance=group, user=request.user, prefix="group")
    if form.is_valid():
        saved = _save(form, services.save_group, request.user)
        if saved is not None:
            moved = saved.group.section_id != section.pk
            message = f"Группа «{saved.group.name}» сохранена" + (
                f" — теперь в разделе «{saved.group.section.name}»." if moved else "."
            )
            # Группу перенесли — остаёмся во вкладке, где её правили: итог виден здесь.
            return _group_panel(request, saved.group, section=section, message=message)
    group.refresh_from_db()
    return _group_panel(request, group, group_form=form)


@require_POST
def group_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """«Удалить» группу — сначала шаг подтверждения в панели, потом действие (решение 82)."""
    services.require_group_schedule(request.user)
    group = _visible_group(request, pk)
    if not request.POST.get("confirm"):
        return _group_panel(request, group, confirm=_confirm(group, "group", group))
    try:
        removed = services.delete_group(request.user, group)
    except services.ScheduleError as error:
        return _group_panel(request, group, error=str(error))
    if not removed.archived:
        return _group_panel(
            request, None, section=group.section, message=f"Группа «{group.name}» удалена."
        )
    group.refresh_from_db()
    return _group_panel(
        request,
        group,
        message=f"Группа «{group.name}» в архиве: её нет в поиске, в листе назначений и в "
        "выборе групп, все её времена в архиве.",
        result=removed.rescheduled,
    )


@require_POST
def group_restore(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    group = _visible_group(request, pk)
    try:
        services.restore_group(request.user, group)
    except services.ScheduleError as error:
        return _group_panel(request, group, error=str(error))
    return _group_panel(
        request,
        group,
        message=f"Группа «{group.name}» снова действует. Её времена в архиве — верните нужные.",
    )


@require_POST
def session_add(request: HttpRequest) -> HttpResponse:
    """Новое время группы (панель группы, «+ время»)."""
    services.require_group_schedule(request.user)
    form = GroupSessionForm(request.POST, user=request.user)
    if form.is_valid():
        group = form.cleaned_data["procedure"]
        result = _save(form, services.save_group_session, request.user)
        if result is not None:
            return _group_panel(request, group, result=result)
    else:
        group = _posted_group(request, form)
        if group is None:
            raise Http404("Группа не найдена.")
    return _group_panel(request, group, new_session_form=form)


def _posted_group(request: HttpRequest, form) -> Procedure | None:
    value = str(form["procedure"].value() or "")
    if not value.isdigit() or len(value) > 18:
        return None
    return services.group_schedule_procedures(request.user).filter(pk=int(value)).first()


def _visible_session(request: HttpRequest, pk: int, *, active_group: bool = True) -> GroupSession:
    groups = (
        services.group_schedule_procedures(request.user)
        if active_group
        else services.schedule_groups(request.user)
    )
    return get_object_or_404(
        GroupSession.objects.filter(procedure__in=groups).select_related("procedure__section"),
        pk=pk,
    )


def session_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    session = _visible_session(request, pk)
    group = session.procedure
    if request.method != "POST":
        return _group_panel(request, group)
    form = _session_form(request, session, data=request.POST)
    if form.is_valid():
        result = _save(form, services.save_group_session, request.user)
        if result is not None:
            return _group_panel(request, group, result=result)
    return _group_panel(request, group, bound_session=form)


@require_POST
def session_delete(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    session = _visible_session(request, pk)
    group = session.procedure
    label = f"{session.start_time:%H:%M}"
    if not request.POST.get("confirm"):
        return _group_panel(request, group, confirm=_confirm(group, "session", session))
    try:
        removed = services.delete_group_session(request.user, session)
    except services.ScheduleError as error:
        return _group_panel(request, group, error=str(error))
    message = (
        f"Время {label} в архиве — пациентов в него больше не ставят."
        if removed.archived
        else f"Время {label} удалено."
    )
    return _group_panel(request, group, message=message, result=removed.rescheduled)


@require_POST
def session_restore(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    session = _visible_session(request, pk, active_group=False)
    group = session.procedure
    try:
        result = services.restore_group_session(request.user, session)
    except services.ScheduleError as error:
        return _group_panel(request, group, error=str(error))
    return _group_panel(
        request, group, message=f"Время {session.start_time:%H:%M} снова действует.", result=result
    )


def _confirm(group: Procedure, kind: str, item: Procedure | GroupSession) -> dict:
    """Шаг подтверждения «Удалить» в панели: удалится совсем или уйдёт в архив, и сколько
    текущих программ пересоберётся."""
    used = services.group_is_used(item) if kind == "group" else services.session_is_used(item)
    url = "scheduling:group_delete" if kind == "group" else "scheduling:session_delete"
    return {
        "kind": kind,
        "item": item,
        "used": used,
        "programs": services.affected_programs(group) if used or kind == "session" else 0,
        "url": reverse(url, args=[item.pk]),
    }


def _session_form(request: HttpRequest, session: GroupSession, data=None) -> GroupSessionForm:
    # Префикс общий («edit»): в каждой строке своя форма; id полей — свои, чтобы не совпадали.
    return GroupSessionForm(
        data,
        instance=session,
        user=request.user,
        prefix="edit",
        auto_id=f"id_s{session.pk}_%s",
    )


def _group_panel(
    request: HttpRequest,
    group: Procedure | None,
    *,
    section: Section | None = None,
    group_form: GroupForm | None = None,
    bound_session: GroupSessionForm | None = None,
    new_session_form: GroupSessionForm | None = None,
    confirm: dict | None = None,
    message: str = "",
    error: str = "",
    result: services.Rescheduled | None = None,
) -> HttpResponse:
    """Панель группы (``group`` и ``group_form`` пусты — панель закрыта) и таблица раздела
    вне основной цели. ``section`` — вкладка, иначе — раздел группы."""
    section = section or (group.section if group else None) or _section(None)
    context: dict[str, Any] = {
        "section": section,
        "group": group,
        "group_form": group_form,
        "confirm": confirm,
        "message": message,
        "error": error,
        "result": services.for_viewer(result, request.user),
    }
    if group is not None:
        group.refresh_from_db()
        if group_form is None:
            group_form = GroupForm(instance=group, user=request.user, prefix="group")
        sessions = sorted(group.sessions.all(), key=lambda s: (s.start_time, s.pk))
        context |= {
            "group_form": group_form,
            "session_rows": [
                (
                    s,
                    bound_session
                    if bound_session is not None and bound_session.instance.pk == s.pk
                    else _session_form(request, s),
                )
                for s in sessions
                if s.is_active
            ],
            "archived_sessions": [s for s in sessions if not s.is_active],
            "new_session_form": new_session_form
            or GroupSessionForm(
                user=request.user,
                initial={"procedure": group, "duration_min": group.default_duration_min or 30},
            ),
        }
    panel = render_to_string("scheduling/_group_panel.html", context, request=request)
    table = render_to_string(
        "scheduling/_group_sessions.html",
        _sessions_context(request, section, selected=group.pk if group else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + table)


def equipment_add(request: HttpRequest) -> HttpResponse:
    services.require_group_schedule(request.user)
    form = NewEquipmentForm(request.POST or None, prefix="neweq")
    if request.method == "POST" and form.is_valid():
        created = _save(
            form,
            lambda user, equipment: services.create_equipment(
                user,
                equipment,
                card_label=form.cleaned_data["card_label"],
                synonyms=form.cleaned_data["synonyms"],
            ),
            request.user,
        )
        if created is not None:
            return _equipment_panel(
                request,
                created,
                message=f"Тренажёр «{created.name}» заведён — его можно назначать пациентам.",
            )
    return _equipment_panel(request, None, new_form=form)


def equipment_edit(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    equipment = get_object_or_404(Equipment, pk=pk)
    form = EquipmentForm(request.POST or None, instance=equipment, prefix="eq")
    if request.method == "POST" and form.is_valid():
        result = _save(
            form,
            lambda user, item: services.save_equipment(
                user,
                item,
                card_label=form.cleaned_data["card_label"],
                synonyms=form.cleaned_data["synonyms"],
            ),
            request.user,
        )
        if result is not None:
            return _equipment_panel(request, equipment, result=result)
    if request.method == "POST":
        equipment.refresh_from_db()
    return _equipment_panel(request, equipment, form=form)


@require_POST
def equipment_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """«Удалить» тренажёр — сначала шаг подтверждения в окне (решение 83)."""
    services.require_group_schedule(request.user)
    equipment = get_object_or_404(Equipment, pk=pk)
    if not request.POST.get("confirm"):
        confirm = {
            "used": services.equipment_is_used(equipment),
            "programs": services.equipment_programs(equipment),
            "url": reverse("scheduling:equipment_delete", args=[equipment.pk]),
        }
        return _equipment_panel(request, equipment, confirm=confirm)
    try:
        removed = services.delete_equipment(request.user, equipment)
    except services.ScheduleError as error:
        return _equipment_panel(request, equipment, error=str(error))
    if not removed.archived:
        return _equipment_panel(request, None, message=f"Тренажёр «{equipment.name}» удалён.")
    equipment.refresh_from_db()
    return _equipment_panel(
        request,
        equipment,
        message=f"Тренажёр «{equipment.name}» в архиве: его нельзя назначить, подбор на него "
        "не ставит.",
        result=removed.rescheduled,
    )


@require_POST
def equipment_restore(request: HttpRequest, pk: int) -> HttpResponse:
    services.require_group_schedule(request.user)
    equipment = get_object_or_404(Equipment, pk=pk)
    result = services.restore_equipment(request.user, equipment)
    equipment.refresh_from_db()
    return _equipment_panel(
        request, equipment, message=f"Тренажёр «{equipment.name}» снова действует.", result=result
    )


def _equipment_panel(
    request: HttpRequest,
    equipment: Equipment | None,
    *,
    form: EquipmentForm | None = None,
    new_form: NewEquipmentForm | None = None,
    message: str = "",
    error: str = "",
    confirm: dict | None = None,
    result: services.Rescheduled | None = None,
) -> HttpResponse:
    if equipment is not None and form is None:
        form = EquipmentForm(instance=equipment, prefix="eq")
    panel = render_to_string(
        "scheduling/_equipment_panel.html",
        {
            "equipment": equipment,
            "form": form,
            "new_form": new_form,
            "message": message,
            "error": error,
            "confirm": confirm,
            "result": services.for_viewer(result, request.user),
        },
        request=request,
    )
    table = render_to_string(
        "scheduling/_equipment.html",
        _equipment_context(selected=equipment.pk if equipment else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + table)


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


def _sessions_context(
    request, section: Section | None, *, selected: int | None = None, oob: bool = False
) -> dict:
    groups = services.section_groups(request.user, section) if section else []
    active = [group for group in groups if group.is_active]
    archived_groups = [group for group in groups if not group.is_active]
    archived_times = [(group, s) for group in active for s in group.archived_sessions]
    return {
        "section": section,
        "groups": active,
        "archived_groups": archived_groups,
        "archived_times": archived_times,
        "archived_count": len(archived_groups) + len(archived_times),
        "selected": selected,
        "oob": oob,
    }


def _equipment_context(*, selected: int | None = None, oob: bool = False) -> dict:
    items = list(Equipment.objects.order_by("name"))
    return {
        "equipment_list": [item for item in items if item.is_active],
        "archived": [item for item in items if not item.is_active],
        "selected": selected,
        "oob": oob,
    }


# --- Шахматка на дату (FR-SCH-14…16) ------------------------------------------------------
# Ячейку открывает специалист ФР: панель ячейки грузится HTMX-запросом; действие возвращает
# панель с итогом и шахматку целиком (hx-swap-oob), ошибка — панель с текстом, статус 200.


def board_view(request: HttpRequest) -> HttpResponse:
    board_days.ensure_boards()
    day = _board_day(request.GET.get("date"))
    return render(request, "scheduling/board.html", {"board": board.board(request.user, day)})


def board_cell(request: HttpRequest) -> HttpResponse:
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Нет права править расписание пациентов.")
    cell = _cell(request, request.GET)
    return _panel(request, cell)


@require_POST
def board_place(request: HttpRequest) -> HttpResponse:
    """Поставить пациента: из списка ячейки, перетаскиванием из блока или из панели пациента
    (ячейка — в поле ``target`` «инструктор:слот»)."""
    cell = _cell(request, _with_target(request.POST))
    program = get_object_or_404(Program, pk=_int(request.POST.get("program")))
    return _act(
        request,
        cell,
        lambda _reason: board.place_patient(
            request.user, program, cell.instructor, cell.slot, cell.day
        ),
        "Пациент поставлен.",
    )


@require_POST
def board_move(request: HttpRequest, pk: int) -> HttpResponse:
    """Перенести пациента в ячейку ``target`` (из панели или перетаскиванием). Итог и ошибки
    показываются в панели ячейки, куда переносили."""
    booking = get_object_or_404(Booking, pk=pk, kind=BookingKind.INDIVIDUAL)
    cell = _cell(request, _with_target(request.POST))
    version = _int(request.POST.get("version"))
    return _act(
        request,
        cell,
        lambda _reason: board.move_patient(
            request.user, booking, version, cell.instructor, cell.slot
        ),
        "Пациент перенесён.",
    )


@require_POST
def board_equipment_move(request: HttpRequest, pk: int) -> HttpResponse:
    """Перенести пациента на другое время тренажёра перетаскиванием (FR-SCH-10a). Сверх
    вместимости или вне окна — панель с вопросом о причине, ошибка — панель с текстом."""
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Нет права править расписание пациентов.")
    booking = get_object_or_404(
        Booking.objects.select_related("program", "equipment"), pk=pk, kind=BookingKind.EQUIPMENT
    )
    try:
        start = time.fromisoformat(request.POST.get("start", "").strip())
    except ValueError:
        raise Http404("Неверное время.") from None
    version = _int(request.POST.get("version"))
    context = {"booking": booking, "start": start, "version": version}
    try:
        board.move_equipment(request.user, booking, version, start, request.POST.get("reason", ""))
    except board.ConfirmationRequired as error:
        return render(
            request, "scheduling/_board_equipment.html", {**context, "confirm": error.violations}
        )
    except board.BoardError as error:
        return render(request, "scheduling/_board_equipment.html", {**context, "error": str(error)})
    return render(
        request,
        "scheduling/_board_done.html",
        {"done": "Время тренажёра изменено.", "board": board.board(request.user, booking.date)},
    )


def _equipment_booking(request: HttpRequest, pk: int) -> Booking:
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Нет права править расписание пациентов.")
    return get_object_or_404(
        Booking.objects.select_related("program", "equipment"), pk=pk, kind=BookingKind.EQUIPMENT
    )


def board_equipment(request: HttpRequest, pk: int) -> HttpResponse:
    """Панель записи на тренажёр по клику: пометка (FR-SCH-10)."""
    booking = _equipment_booking(request, pk)
    return render(
        request,
        "scheduling/_board_equipment.html",
        {"booking": booking, "version": booking.version, "note_form": True},
    )


@require_POST
def board_equipment_note(request: HttpRequest, pk: int) -> HttpResponse:
    booking = _equipment_booking(request, pk)
    version = _int(request.POST.get("version"))
    try:
        board.set_equipment_note(request.user, booking, version, request.POST.get("note", ""))
    except board.BoardError as error:
        context = {"booking": booking, "version": version, "note_form": True, "error": str(error)}
        return render(request, "scheduling/_board_equipment.html", context)
    return render(
        request,
        "scheduling/_board_done.html",
        {"done": "Пометка сохранена.", "board": board.board(request.user, booking.date)},
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
        "Пациент убран из сетки — он в блоке «Отменены».",
    )


@require_POST
def board_note(request: HttpRequest, pk: int) -> HttpResponse:
    booking = get_object_or_404(Booking, pk=pk, kind=BookingKind.INDIVIDUAL)
    cell = _cell(request, request.POST)
    version = _int(request.POST.get("version"))
    return _act(
        request,
        cell,
        lambda _reason: board.set_note(
            request.user, booking, version, request.POST.get("note", "")
        ),
        "Пометка сохранена.",
    )


def board_patient(request: HttpRequest) -> HttpResponse:
    """Панель пациента из блока «Не распределены» / «Отменены»: выбрать ячейку и поставить."""
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Нет права править расписание пациентов.")
    day = _board_day(request.GET.get("date"))
    candidate = next(
        (
            item
            for item in board.programs_for_cell(request.user, day)
            if item.program.pk == _int(request.GET.get("program"))
        ),
        None,
    )
    if candidate is None:
        raise Http404("Пациента нет среди пациентов на лечении в этот день.")
    return render(
        request,
        "scheduling/_board_patient.html",
        {
            "day": day,
            "candidate": candidate,
            "targets": _targets(day),
            "busy": board.busy_json(
                board.occupied({candidate.program.pk}, day).get(candidate.program.pk, [])
            ),
        },
    )


def _targets(day: date) -> list[tuple[str, str, InstructorSlot]]:
    """Ячейки для списка «Перенести в…»; слот — чтобы подсветить время, где пациент занят."""
    return [
        (
            f"{instructor.pk}:{slot.pk}",
            f"{slot.start:%H:%M} — {instructor.short_name}"
            + (" (вечер)" if slot.is_evening else ""),
            slot,
        )
        for instructor, slot in board.free_seats(day)
    ]


def _with_target(data) -> dict:
    """«инструктор:слот» из списка ячеек — в поля instructor и slot."""
    values = {key: data.get(key, "") for key in ("date", "instructor", "slot")}
    target = data.get("target", "")
    if target:
        values["instructor"], _sep, values["slot"] = target.partition(":")
    return values


@require_POST
def board_block(request: HttpRequest) -> HttpResponse:
    cell = _cell(request, request.POST)
    until = request.POST.get("until", "").strip()
    try:
        until_day = date.fromisoformat(until) if until else None
    except ValueError:
        return _panel(request, cell, error="Неверная дата «по дату».")

    session = _session(request.POST)

    def action(_reason: str) -> None:
        result = board.add_block(
            request.user,
            cell.instructor,
            cell.slot,
            cell.day,
            request.POST.get("kind", ""),
            request.POST.get("label", ""),
            until_day,
            group_session=session,
            with_partner=bool(request.POST.get("partner")),
        )
        if result.skipped:
            days = ", ".join(f"{day:%d.%m}" for day in result.skipped)
            raise _Done(f"Блок поставлен. Пропущены даты с пациентом в ячейке: {days}.")

    return _act(request, cell, action, "Блок поставлен.")


@require_POST
def board_duty(request: HttpRequest) -> HttpResponse:
    """Постоянный распорядок из ячейки: с даты ячейки, бессрочно или по дату (FR-STF-5)."""
    cell = _cell(request, request.POST)
    until = request.POST.get("until", "").strip()
    try:
        until_day = date.fromisoformat(until) if until else None
    except ValueError:
        return _panel(request, cell, error="Неверная дата «по дату».")
    session = _session(request.POST)
    return _act(
        request,
        cell,
        lambda _reason: board.add_duty(
            request.user,
            cell.instructor,
            cell.slot,
            cell.day,
            request.POST.get("kind", ""),
            request.POST.get("label", ""),
            until_day,
            group_session=session,
            with_partner=bool(request.POST.get("partner")),
        ),
        f"Распорядок поставлен с {cell.day:%d.%m}.",
    )


@require_POST
def board_duty_end(request: HttpRequest) -> HttpResponse:
    """Завершить постоянный распорядок в ячейке с этой даты."""
    cell = _cell(request, request.POST)
    return _act(
        request,
        cell,
        lambda _reason: board.end_duty(
            request.user,
            cell.instructor,
            cell.slot,
            cell.day,
            with_partner=bool(request.POST.get("partner")),
        ),
        f"Распорядок завершён: последний день — {cell.day - timedelta(days=1):%d.%m}.",
    )


def _session(data) -> GroupSession | None:
    value = data.get("session", "").strip()
    if not value:
        return None
    return get_object_or_404(GroupSession, pk=_int(value), is_active=True)


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
    """Дата из адреса; без даты — сегодня, а в выходной — следующий будний день. Вперёд —
    не дальше завтрашней шахматки: будущих шахматок нет, листать туда нечего (FR-SCH-14)."""
    dates = board_days.board_dates(board_days.today())
    try:
        if value:
            return min(date.fromisoformat(value), dates[-1])
    except ValueError:
        pass
    return dates[0]


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
        raise PermissionDenied("Нет права править расписание пациентов.")
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
    patients = [
        (patient, board.can_edit_patient(request.user, patient.booking.program))
        for patient in (seat.patients if seat else [])
    ]
    can_place = seat is not None and seat.busy is None and current.can_edit
    candidates = board.programs_for_cell(request.user, cell.day) if can_place else []
    targets = _targets(cell.day) if any(ok for _p, ok in patients) else []
    return render(
        request,
        "scheduling/_board_cell.html",
        {
            "cell": cell,
            "seat": seat,
            "weekend": current.is_weekend,
            "can_edit_day": current.can_edit,
            "patients": patients,
            "candidates": candidates,
            "many_departments": len({c.program.department_id for c in candidates}) > 1,
            "targets": targets,
            "block_kinds": [(k.value, k.label) for k in board.CELL_BLOCK_KINDS],
            "sessions": board.group_sessions_for(cell.slot),
            "duty": board.duty_at(cell.instructor, cell.slot, cell.day),
            "partner": cell.instructor.partner
            if cell.instructor.partner_id and cell.instructor.partner.is_active
            else None,
            "error": error,
            "confirm": confirm or [],
            "posted": request.POST,
        },
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
    """«Вернуть» убранное занятие на дату."""
    program = get_program_or_404(request.user, pk)
    prescription = get_object_or_404(Prescription, pk=prescription_pk, program=program)
    try:
        day = date.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        raise Http404("Неверная дата.") from None
    try:
        with services.watch_displaced(program) as displaced:
            manual.restore_date(request.user, prescription, day)
    except manual.EditError as error:
        if not manual.can_edit(request.user, program):
            raise PermissionDenied(str(error)) from None
        return _booking_done(request, program, "", error=str(error))
    return _booking_done(
        request, program, "Занятие возвращено — подбор поставил его снова.", displaced=displaced
    )


def _program_booking(request: HttpRequest, pk: int) -> Booking:
    """Группа, бассейн или тренажёр программы, которую пользователь видит; чужое — 404
    (FR-ACC-2). Индивидуальные на странице программы не правятся — только в шахматке
    (FR-SCH-16)."""
    booking = get_object_or_404(
        Booking.objects.select_related(
            "program", "procedure", "equipment", "group_session"
        ).exclude(kind=BookingKind.INDIVIDUAL),
        pk=pk,
    )
    get_program_or_404(request.user, booking.program_id)
    return booking


def _target(booking: Booking, data) -> manual.Target:
    if booking.kind in GROUP_BOOKING_KINDS:
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
        with services.watch_displaced(booking.program) as displaced:
            result = action(reason)
    except manual.ConfirmationRequired as error:
        return _booking_panel(request, booking, confirm=error.violations)
    except manual.EditError as error:
        if not manual.can_edit(request.user, booking.program):
            raise PermissionDenied(str(error)) from None
        return _booking_panel(request, booking, error=str(error))
    skipped = getattr(result, "skipped", [])
    return _booking_done(request, booking.program, done, skipped=skipped, displaced=displaced)


def _booking_done(
    request: HttpRequest,
    program: Program,
    done: str,
    *,
    error: str = "",
    skipped=(),
    displaced: list[services.Displaced] | None = None,
) -> HttpResponse:
    program.refresh_from_db()
    return render(
        request,
        "scheduling/_booking_done.html",
        {
            "done": done,
            "error": error,
            "skipped": skipped,
            "displaced": displaced or [],
            "program": program,
            "schedule": services.program_schedule(program),
            "can_schedule": services.can_schedule(request.user, program),
        },
    )


def _booking_panel(
    request: HttpRequest, booking: Booking, *, error: str = "", confirm: list | None = None
) -> HttpResponse:
    if not manual.can_edit(request.user, booking.program):
        raise PermissionDenied("Нет права править расписание пациентов.")
    current = (
        Booking.objects.filter(pk=booking.pk)
        .select_related("procedure", "equipment", "group_session")
        .first()
    )
    if current is None:
        return _booking_done(
            request, booking.program, "", error="Занятие уже изменено — обновите страницу."
        )
    options: list[tuple[str, str]] = []
    selected = ""
    if current.kind in GROUP_BOOKING_KINDS:
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
            "options": options,
            "selected": request.POST.get("session") or request.POST.get("start") or selected,
            "field": "start" if current.kind == BookingKind.EQUIPMENT else "session",
            "error": error,
            "confirm": confirm or [],
            "posted": request.POST,
        },
    )


# --- Инструктор не работает (FR-STF-6) -----------------------------------------------------


@require_POST
def board_off(request: HttpRequest) -> HttpResponse:
    """«Не работает» из шахматки одним действием: исключение смены, его пациенты — в
    «Не распределены» (FR-STF-6)."""
    if not board.can_edit_board(request.user):
        raise PermissionDenied("Нет права править расписание пациентов.")
    instructor = get_object_or_404(Instructor, pk=_int(request.POST.get("instructor")))
    day = _board_day(request.POST.get("date"))
    staff_changes.set_not_working(request.user, instructor, day)
    messages.success(
        request,
        f"{instructor.short_name}: {day:%d.%m} не работает — его пациенты в «Не распределены».",
    )
    return redirect(f"{reverse('scheduling:board')}?date={day:%Y-%m-%d}")


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
    filename = f"Расписание {day:%d.%m.%Y}.xlsx"
    response["Content-Disposition"] = (
        f"attachment; filename=\"board.xlsx\"; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_not_required
def public_board(request: HttpRequest) -> HttpResponse:
    """Публичная шахматка (FR-ACC-5): без входа, только чтение, только из сетей клиники."""
    if not public_board_allowed(request):
        raise Http404("Страница не найдена.")
    board_days.ensure_boards()
    day = _board_day(request.GET.get("date"))
    return render(request, "scheduling/public_board.html", {"board": board.board(None, day)})


def client_address(request: HttpRequest) -> str:
    """За nginx адрес клиента — в X-Real-IP (BEHIND_PROXY), иначе REMOTE_ADDR."""
    if settings.BEHIND_PROXY:
        return request.META.get("HTTP_X_REAL_IP", "")
    return request.META.get("REMOTE_ADDR", "")


def public_board_allowed(request: HttpRequest) -> bool:
    networks = [item.strip() for item in settings.PUBLIC_BOARD_NETWORKS if item.strip()]
    if not networks:
        return True  # локальная разработка: настройка не задана
    try:
        address = ipaddress.ip_address(client_address(request))
        return any(address in ipaddress.ip_network(net, strict=False) for net in networks)
    except ValueError:
        return False
