from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .events import origin

# Номер вкладки (случайный, задаёт static/js/live.js): своё событие вкладка не перерисовывает.
CLIENT_HEADER = "HTTP_X_LIVE_CLIENT"


class LiveOriginMiddleware:
    """Кто делает правку — для подсказки «Шахматку изменила Петрова Б.Б.» (TZ.md NFR-11)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        by = ""
        if user is not None and user.is_authenticated:
            by = str(user)
        client = request.META.get(CLIENT_HEADER, "")[:40]
        token = origin.set((by, client))
        try:
            return self.get_response(request)
        finally:
            origin.reset(token)
