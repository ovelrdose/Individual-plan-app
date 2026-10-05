from urllib.parse import quote

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

from apps.programs.services import get_program_or_404

from .services import export_card
from .xlsx.writer import CardRenderError

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def card_download(request: HttpRequest, pk: int) -> HttpResponse:
    """Карта программы в .xlsx для печати из MS Excel (TZ.md §8). Доступна всем,
    кто видит программу."""
    program = get_program_or_404(request.user, pk)
    try:
        filename, content = export_card(request.user, program)
    except CardRenderError as error:
        messages.error(request, str(error))
        return redirect("programs:detail", pk=pk)
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = _attachment(filename)
    return response


def _attachment(filename: str) -> str:
    # Русское имя файла — по RFC 5987, чтобы браузеры не превращали его в «___.xlsx».
    return f"attachment; filename=\"card.xlsx\"; filename*=UTF-8''{quote(filename)}"
