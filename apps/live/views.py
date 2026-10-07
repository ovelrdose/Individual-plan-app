"""Поток событий ``/events/`` (Server-Sent Events, TZ.md NFR-11)."""

import queue
from collections.abc import Iterator

from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpRequest, StreamingHttpResponse

from apps.scheduling.views import public_board_allowed

from . import topics
from .events import broker

# Пульс: прокси не закрывают молчащее соединение, а закрытая вкладка освобождает поток.
HEARTBEAT_SECONDS = 25


@login_not_required
def events(request: HttpRequest) -> StreamingHttpResponse:
    """Темы — в адресе: «?topics=board:2026-10-07,program:13». Без входа — только шахматки
    и только из сетей публичной шахматки, без подписи автора (FR-SCH-15)."""
    signed_in = request.user.is_authenticated
    if not signed_in and not public_board_allowed(request):
        raise Http404("Страница не найдена.")
    wanted = topics.parse(
        request.GET.get("topics", ""), allowed=None if signed_in else (topics.BOARD,)
    )
    response = StreamingHttpResponse(
        stream(wanted, signed_in=signed_in), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    # nginx не должен копить поток в буфере (фаза 5).
    response["X-Accel-Buffering"] = "no"
    return response


def stream(
    wanted: list[str], *, signed_in: bool, heartbeat: float = HEARTBEAT_SECONDS
) -> Iterator[str]:
    box = broker.subscribe()
    try:
        yield "retry: 5000\n\n"
        while True:
            try:
                event = box.get(timeout=heartbeat)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if not topics.matches(wanted, event.topics):
                continue
            if not signed_in:
                event = type(event)(event.topics)
            yield f"data: {event.to_json()}\n\n"
    finally:
        broker.unsubscribe(box)
