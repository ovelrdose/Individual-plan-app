from collections.abc import Callable
from datetime import date
from typing import Any

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.access import (
    current_department,
    departments_for,
    get_department_or_404,
    has_right,
    is_admin,
)
from apps.accounts.models import Right, User
from apps.scheduling import staff_changes
from apps.scheduling.services import for_viewer
from apps.staff import services as staff_services
from apps.staff.forms import InstructorForm
from apps.staff.models import Instructor

from . import services
from .forms import ProcedureForm
from .models import InstructorSlot, Procedure, ProcedureKind
from .services import search_procedures


def procedure_search(request: HttpRequest) -> HttpResponse:
    """Подсказки для поля «процедура» (HTMX): поиск по названию и синонимам.

    Отделение передаётся явно — пациент может быть не из отделения, выбранного в шапке.
    """
    department_id = request.GET.get("department")
    if department_id:
        department = get_department_or_404(request.user, department_id)
    else:
        department = current_department(request)
    query = request.GET.get("q", "")
    procedures = search_procedures(department, query) if department else []
    return render(
        request, "catalog/_procedure_search.html", {"procedures": procedures, "query": query}
    )


# --- Экран «Справочники» (TZ.md FR-CAT-7, решение 83) ---------------------------------------
# Таблицы — только просмотр; шестерёнка открывает окно правки (#edit-modal, как «Групповые
# занятия»): действие возвращает содержимое окна, таблицу — через hx-swap-oob.

TAB_LABELS = [
    ("procedures", "Процедуры"),
    ("instructors", "Инструкторы"),
    ("slots", "Сетка слотов"),
    ("exports", "Журнал выгрузок карт"),
]


def _tabs(user: User) -> list[str]:
    """Вкладки по правам: справочники — «Справочники», журнал выгрузок — «Пользователи»
    (в журнале ФИО пациентов)."""
    tabs = ["procedures", "instructors", "slots"] if services.can_edit_catalog(user) else []
    if has_right(user, Right.USERS):
        tabs.append("exports")
    return tabs


def catalog(request: HttpRequest) -> HttpResponse:
    tabs = _tabs(request.user)
    if not tabs:
        raise PermissionDenied("Нет права на справочники.")
    tab = request.GET.get("tab") if request.GET.get("tab") in tabs else tabs[0]
    context: dict[str, Any] = {"tab": tab, "tabs": tabs, "tab_labels": TAB_LABELS}
    if tab == "procedures":
        context |= _procedures_context(request)
    elif tab == "instructors":
        context |= _instructors_context()
    elif tab == "slots":
        context["slots"] = InstructorSlot.objects.order_by("start")
    else:
        context |= _exports_context(request)
    return render(request, "catalog/catalog.html", context)


# --- Процедуры ----------------------------------------------------------------------------


KIND_FILTERS = [
    ("", "Все, кроме групп"),
    (ProcedureKind.INDIVIDUAL.value, "Индивидуальные"),
    (ProcedureKind.CARD_ONLY.value, "Только строка в карте"),
    (ProcedureKind.POOL.value, "Бассейн без группы"),
    (ProcedureKind.EQUIPMENT.value, "Тренажёры"),
    ("GROUPS", "Группы (правятся в «Групповых занятиях»)"),
]


def _procedures_context(
    request: HttpRequest, *, selected: int | None = None, oob: bool = False
) -> dict:
    data = request.GET if request.method == "GET" else request.POST
    kind = data.get("kind", "") if data.get("kind", "") in dict(KIND_FILTERS) else ""
    query = data.get("q", "").strip()
    items = services.filter_procedures(services.catalog_procedures(request.user), kind, query)
    return {
        "procedures": [p for p in items if p.is_active],
        "archived": [p for p in items if not p.is_active],
        "kind": kind,
        "query": query,
        "kind_filters": KIND_FILTERS,
        "selected": selected,
        "oob": oob,
    }


def _visible_procedure(request: HttpRequest, pk: int) -> Procedure:
    services.require_catalog(request.user)
    return get_object_or_404(services.catalog_procedures(request.user), pk=pk)


def procedure_add(request: HttpRequest) -> HttpResponse:
    services.require_catalog(request.user)
    form = ProcedureForm(
        request.POST or None,
        user=request.user,
        prefix="proc",
        initial={"kind": ProcedureKind.CARD_ONLY},
    )
    if request.method == "POST" and form.is_valid():
        saved = _save(form, services.create_procedure, request.user)
        if saved is not None:
            return _procedure_panel(request, saved, message=f"Процедура «{saved.name}» заведена.")
    return _procedure_panel(request, None, form=form)


def procedure_edit(request: HttpRequest, pk: int) -> HttpResponse:
    procedure = _visible_procedure(request, pk)
    if request.method != "POST":
        return _procedure_panel(request, procedure)
    form = ProcedureForm(request.POST, instance=procedure, user=request.user, prefix="proc")
    if form.is_valid():
        saved = _save(form, services.save_procedure, request.user)
        if saved is not None:
            return _procedure_panel(request, saved, message="Сохранено.")
    procedure.refresh_from_db()
    return _procedure_panel(request, procedure, form=form)


@require_POST
def procedure_delete(request: HttpRequest, pk: int) -> HttpResponse:
    procedure = _visible_procedure(request, pk)
    if not request.POST.get("confirm"):
        return _procedure_panel(request, procedure, confirm=_procedure_confirm(procedure))
    try:
        removed = services.delete_procedure(request.user, procedure)
    except services.CatalogError as error:
        return _procedure_panel(request, procedure, error=str(error))
    if not removed.archived:
        return _procedure_panel(
            request, None, message=f"Процедура «{procedure.name}» удалена.", closed=True
        )
    procedure.refresh_from_db()
    return _procedure_panel(
        request,
        procedure,
        message=f"«{procedure.name}» в архиве: её нет в поиске и в распознавании листа.",
        result=removed.rescheduled,
    )


@require_POST
def procedure_restore(request: HttpRequest, pk: int) -> HttpResponse:
    procedure = _visible_procedure(request, pk)
    try:
        restored = services.restore_procedure(request.user, procedure)
    except services.CatalogError as error:
        return _procedure_panel(request, procedure, error=str(error))
    procedure.refresh_from_db()
    return _procedure_panel(
        request,
        procedure,
        message=f"«{procedure.name}» снова действует.",
        result=restored.rescheduled,
    )


def _procedure_confirm(procedure: Procedure) -> dict:
    from apps.scheduling.services import equipment_is_used, equipment_programs

    if procedure.kind == ProcedureKind.EQUIPMENT:
        used = equipment_is_used(procedure.equipment)
        programs: int | None = equipment_programs(procedure.equipment)
    else:
        used, programs = services.procedure_is_used(procedure), None
    details = []
    if used and procedure.kind == ProcedureKind.CARD_ONLY and not procedure.evening_individual:
        details.append("У назначений строка в карте останется.")
    return {
        "used": used,
        "programs": programs,
        "details": details,
        "url": reverse("catalog:procedure_delete", args=[procedure.pk]),
    }


def _procedure_panel(
    request: HttpRequest,
    procedure: Procedure | None,
    *,
    form: ProcedureForm | None = None,
    message: str = "",
    error: str = "",
    confirm: dict | None = None,
    result: object | None = None,
    closed: bool = False,
) -> HttpResponse:
    # Процедура отделения в архиве — только просмотр: правка её отделения невозможна.
    readonly = (
        procedure is not None
        and procedure.department_id is not None
        and not procedure.department.is_active
    )
    if procedure is not None and form is None and not procedure.is_group and not readonly:
        form = ProcedureForm(instance=procedure, user=request.user, prefix="proc")
    panel = render_to_string(
        "catalog/_procedure_panel.html",
        {
            "procedure": None if closed else procedure,
            "readonly": readonly,
            "form": None if closed else form,
            "message": message,
            "error": error,
            "confirm": confirm,
            "result": for_viewer(result, request.user),
        },
        request=request,
    )
    table = render_to_string(
        "catalog/_procedures.html",
        _procedures_context(request, selected=procedure.pk if procedure else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + table)


# --- Инструкторы --------------------------------------------------------------------------


def _instructors_context(*, selected: int | None = None, oob: bool = False) -> dict:
    items = list(
        Instructor.objects.select_related("partner").order_by("display_order", "short_name")
    )
    return {
        "instructors": [i for i in items if i.is_active],
        "archived": [i for i in items if not i.is_active],
        "selected": selected,
        "oob": oob,
    }


def _instructor(request: HttpRequest, pk: int) -> Instructor:
    staff_services.require_instructors(request.user)
    return get_object_or_404(Instructor, pk=pk)


def instructor_add(request: HttpRequest) -> HttpResponse:
    staff_services.require_instructors(request.user)
    form = InstructorForm(request.POST or None, prefix="ins")
    if request.method == "POST" and form.is_valid():
        saved = _save_instructor(request, form)
        if saved is not None:
            return _instructor_panel(
                request,
                saved,
                message=f"Инструктор «{saved.short_name}» заведён. Шаблон смены задайте на "
                "экране «Смены».",
            )
    return _instructor_panel(request, None, form=form)


def instructor_edit(request: HttpRequest, pk: int) -> HttpResponse:
    instructor = _instructor(request, pk)
    if request.method != "POST":
        return _instructor_panel(request, instructor)
    form = InstructorForm(request.POST, instance=instructor, prefix="ins")
    if form.is_valid():
        saved = _save_instructor(request, form)
        if saved is not None:
            return _instructor_panel(request, saved, message="Сохранено.")
    instructor.refresh_from_db()
    return _instructor_panel(request, instructor, form=form)


def _save_instructor(request: HttpRequest, form: InstructorForm) -> Instructor | None:
    return _save(
        form,
        lambda user, item: staff_services.save_instructor_card(
            user, item, form.cleaned_data["partner"]
        ),
        request.user,
    )


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
            "url": reverse("catalog:instructor_delete", args=[instructor.pk]),
        }
        return _instructor_panel(request, instructor, confirm=confirm)
    try:
        removed = staff_changes.delete_instructor(request.user, instructor)
    except staff_services.StaffError as error:
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
        message=f"«{instructor.short_name}» снова работает. Задайте ему шаблон смены на экране "
        "«Смены», напарника — здесь.",
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
        form = InstructorForm(instance=instructor, prefix="ins")
    displaced, hidden = [], 0
    if removed is not None:
        # Фамилии — только пациентов своих отделений (FR-ACC-2), чужие — числом.
        own = set(departments_for(request.user).values_list("pk", flat=True))
        displaced = [d for d in removed.displaced if d.program.department_id in own]
        hidden = len({d.program.pk for d in removed.displaced} - {d.program.pk for d in displaced})
    panel = render_to_string(
        "catalog/_instructor_panel.html",
        {
            "instructor": instructor,
            "form": None if closed else form,
            "message": message,
            "error": error,
            "confirm": confirm,
            "removed": removed,
            "displaced": displaced,
            "hidden": hidden,
        },
        request=request,
    )
    table = render_to_string(
        "catalog/_instructors.html",
        _instructors_context(selected=instructor.pk if instructor else None, oob=True),
        request=request,
    )
    return HttpResponse(panel + table)


# --- Журнал выгрузок карт (FR-CRD-5) ------------------------------------------------------


def _exports_context(request: HttpRequest) -> dict:
    """Последние выгрузки с фильтром по дате и поиском по ФИО (не больше 300 строк)."""
    from apps.cards.models import CardExport

    exports = CardExport.objects.select_related("program__department", "user")
    if not is_admin(request.user):
        exports = exports.filter(program__department__in=departments_for(request.user))
    day = _date(request.GET.get("date"))
    if day is not None:
        exports = exports.filter(created_at__date=day)
    query = request.GET.get("q", "").strip()
    if query:
        exports = exports.filter(program__full_name__icontains=query)
    return {"exports": exports[:300], "export_date": day, "query": query}


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _save(form: forms.ModelForm, action: Callable[[User, Any], Any], user: User) -> Any | None:
    """Сервис → ошибки в форме: понятный текст сервиса или проверки модели."""
    try:
        return action(user, form.instance)
    except (services.CatalogError, staff_services.StaffError) as error:
        form.add_error(None, str(error))
        return None
    except ValidationError as error:
        for field, messages_ in error.message_dict.items():
            form.add_error(field if field in form.fields else None, messages_)
        return None
