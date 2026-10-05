from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.accounts.access import current_department, get_department_or_404

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
    procedures = search_procedures(department, request.GET.get("q", "")) if department else []
    return render(request, "catalog/_procedure_search.html", {"procedures": procedures})
