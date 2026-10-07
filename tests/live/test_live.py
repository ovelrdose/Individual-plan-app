"""Обновление в реальном времени (TZ.md NFR-11): темы, события после фиксации, поток /events/.

Сегодня в тестах — вторник 06.10.2026, следующий будний — среда 07.10.
"""

import queue
import time
from datetime import date

import pytest
from django.db import transaction
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from apps.live import events, topics
from apps.live.events import Broker, Event, broker, origin
from apps.live.middleware import LiveOriginMiddleware
from apps.live.views import stream
from apps.programs.models import Prescription
from apps.programs.services import update_prescription
from apps.scheduling import board, board_days
from apps.staff.models import InstructorBlock

TUE, WED = date(2026, 10, 6), date(2026, 10, 7)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(board_days, "today", lambda: TUE)


@pytest.fixture
def sent(monkeypatch):
    """События, которые ушли бы в PostgreSQL."""
    items: list[Event] = []
    monkeypatch.setattr(events, "send", items.append)
    return items


def commit_setup(items: list[Event]) -> None:
    """Тестовая транзакция не фиксируется: отправки, отложенные подготовкой данных (фикстуры),
    выполняем сами, чтобы проверка видела только событие проверяемой правки."""
    conn = transaction.get_connection()
    for _sids, func, _robust in conn.run_on_commit:
        func()
    conn.run_on_commit.clear()
    items.clear()


class TestTopics:
    @pytest.mark.parametrize(
        ("wanted", "changed", "hit"),
        [
            (["board:2026-10-07"], ["board:2026-10-07"], True),
            (["board:2026-10-07"], ["board:2026-10-06"], False),
            (["board:2026-10-07"], ["board"], True),
            (["board"], ["board:2026-10-06"], True),
            (["program:1"], ["program:12"], False),
            (["programs", "program:1"], ["programs"], True),
            ([], ["board"], False),
        ],
    )
    def test_matches(self, wanted, changed, hit):
        assert topics.matches(wanted, changed) is hit

    def test_parse(self):
        assert topics.parse(" board:2026-10-07, ,program:3") == ["board:2026-10-07", "program:3"]
        assert topics.parse("board:1,program:3", allowed=(topics.BOARD,)) == ["board:1"]
        assert topics.parse("x" * 50) == []
        assert len(topics.parse(",".join(f"program:{i}" for i in range(20)))) == 10


class TestPublish:
    def test_one_event_after_commit(self, sent, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            events.publish("board:2026-10-07")
            events.publish("program:1", "board:2026-10-07")
            assert sent == []  # до фиксации — ничего
        assert [e.topics for e in sent] == [("board:2026-10-07", "program:1")]

    def test_rollback_sends_nothing(self, sent, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            try:
                with transaction.atomic():
                    events.publish("programs")
                    raise ValueError("откат")
            except ValueError:
                pass
            # Темы откаченной транзакции не уходят со следующей правкой.
            events.publish("board:2026-10-07")
        assert [e.topics for e in sent] == [("board:2026-10-07",)]

    def test_send_failure_does_not_break_edit(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        def broken(event):
            raise RuntimeError("нет соединения")

        monkeypatch.setattr(events, "send", broken)
        with django_capture_on_commit_callbacks(execute=True):
            events.publish("programs")  # исключение поглощено и записано в лог

    def test_board_edit_topics_and_author(
        self, sent, patient, rehab, staff, slots, django_capture_on_commit_callbacks
    ):
        board_days.ensure_boards()
        program = patient()
        commit_setup(sent)
        token = origin.set(("Петрова Б.Б.", "tab-1"))
        try:
            with django_capture_on_commit_callbacks(execute=True):
                board.place_patient(rehab, program, staff["sokolov"], slots["9:50"], TUE)
        finally:
            origin.reset(token)

        event = sent[-1]
        assert {topics.board(TUE), topics.program(program.pk)} <= set(event.topics)
        assert (event.by, event.client) == ("Петрова Б.Б.", "tab-1")

    def test_prescription_and_staff_topics(
        self, sent, patient, doctor, staff, slots, django_capture_on_commit_callbacks
    ):
        board_days.ensure_boards()
        program = patient()
        commit_setup(sent)
        item = Prescription.objects.filter(program=program).first()
        with django_capture_on_commit_callbacks(execute=True):
            update_prescription(doctor, item)
        assert {topics.program(program.pk), topics.PROGRAMS, topics.board(WED)} <= set(
            sent[-1].topics
        )

        commit_setup(sent)
        with django_capture_on_commit_callbacks(execute=True):
            InstructorBlock.objects.create(
                instructor=staff["sokolov"], date=WED, slot=slots["9:10"], kind="METHOD_WORK"
            )
        assert topics.BOARD in sent[-1].topics


class TestStream:
    def test_stream_filters_and_heartbeat(self, monkeypatch):
        local = Broker()
        boxes: list[queue.Queue] = []

        def subscribe():
            box: queue.Queue = queue.Queue()
            boxes.append(box)
            return box

        monkeypatch.setattr(local, "subscribe", subscribe)
        monkeypatch.setattr("apps.live.views.broker", local)

        chunks = stream(["board:2026-10-07"], signed_in=False, heartbeat=0.05)
        assert next(chunks) == "retry: 5000\n\n"
        assert next(chunks) == ": ping\n\n"
        boxes[0].put(Event(("program:1",), "Петрова Б.Б.", "t"))
        boxes[0].put(Event(("board:2026-10-07",), "Петрова Б.Б.", "t"))
        # Чужая тема пропущена; без входа — без подписи автора.
        assert next(chunks) == 'data: {"topics": ["board:2026-10-07"], "by": "", "client": ""}\n\n'
        chunks.close()

    def test_dispatch_drops_oldest_when_full(self):
        local = Broker()
        box: queue.Queue = queue.Queue(maxsize=1)
        local._queues.add(box)
        local.dispatch(Event(("a",)))
        local.dispatch(Event(("b",)))
        assert box.get_nowait().topics == ("b",)
        local.unsubscribe(box)
        assert not local._queues


class TestView:
    def test_signed_in_stream(self, client, rehab, monkeypatch):
        monkeypatch.setattr("apps.live.views.stream", lambda wanted, signed_in: iter([str(wanted)]))
        client.force_login(rehab)
        response = client.get(reverse("live_events"), {"topics": "board:2026-10-07,program:5"})
        assert response["Content-Type"] == "text/event-stream"
        assert response["X-Accel-Buffering"] == "no"
        assert b"".join(response.streaming_content) == b"['board:2026-10-07', 'program:5']"

    def test_anonymous_only_boards_inside_network(self, client, settings, monkeypatch, db):
        monkeypatch.setattr(
            "apps.live.views.stream", lambda wanted, signed_in: iter([f"{wanted}{signed_in}"])
        )
        settings.PUBLIC_BOARD_NETWORKS = []
        response = client.get(reverse("live_events"), {"topics": "board:2026-10-07,program:5"})
        assert b"".join(response.streaming_content) == b"['board:2026-10-07']False"

        settings.PUBLIC_BOARD_NETWORKS = ["10.0.0.0/8"]
        assert client.get(reverse("live_events")).status_code == 404


class TestMiddleware:
    def test_origin_is_set_for_request_only(self, rehab):
        seen = []

        def view(request):
            seen.append(origin.get())
            return HttpResponse()

        request = RequestFactory().get("/", HTTP_X_LIVE_CLIENT="tab-7")
        request.user = rehab
        LiveOriginMiddleware(view)(request)
        assert seen == [(str(rehab), "tab-7")] and origin.get() == ("", "")


@pytest.mark.django_db(transaction=True)
def test_notify_reaches_listener():
    """Настоящий LISTEN/NOTIFY: событие после фиксации доходит до подписчика."""
    box = broker.subscribe()
    try:
        time.sleep(0.5)  # соединение слушателя успевает выполнить LISTEN
        with transaction.atomic():
            events.publish("board:2026-10-07")
        event = box.get(timeout=5)
        assert event.topics == ("board:2026-10-07",)
    finally:
        listener = broker._thread
        broker.unsubscribe(box)
        # Слушатель закрывает соединение, иначе тестовую базу не удалить.
        if listener is not None:
            listener.join(timeout=5)
