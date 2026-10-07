"""События об изменениях для обновления страниц в реальном времени (TZ.md NFR-11).

После фиксации транзакции в PostgreSQL уходит ``NOTIFY`` со списком тем, автором и номером
вкладки, из которой сделана правка. В каждом процессе одно соединение слушает канал и
раздаёт события подписчикам — потокам ``/events/`` открытых страниц. Redis и брокеры не нужны.
"""

import json
import logging
import queue
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass

import psycopg
from django.db import connections, transaction

logger = logging.getLogger(__name__)

CHANNEL = "omr_events"
# Кто правит: подпись пользователя и вкладка — ставит middleware на время запроса.
origin: ContextVar[tuple[str, str]] = ContextVar("live_origin", default=("", ""))


@dataclass(frozen=True)
class Event:
    topics: tuple[str, ...]
    by: str = ""
    client: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"topics": list(self.topics), "by": self.by, "client": self.client},
            ensure_ascii=False,
        )


def publish(*topics: str) -> None:
    """Темы уходят одним уведомлением после фиксации транзакции: откат ничего не рассылает,
    а десятки сохранений одной правки дают одно событие."""
    conn = transaction.get_connection()
    pending = _pending()
    # Отправка уже ждёт фиксации — просто добавляем темы. Если не ждёт, набор остался от
    # откаченной транзакции (Django при откате снимает её on_commit) — начинаем заново.
    waiting = conn.in_atomic_block and any(item[1] is _flush for item in conn.run_on_commit)
    if not waiting:
        pending.clear()
    pending.update(topics)
    if not waiting:
        transaction.on_commit(_flush)


def _pending() -> set[str]:
    # Набор — на соединении потока: у каждого запроса своя транзакция.
    return transaction.get_connection().__dict__.setdefault("live_topics", set())


def _flush() -> None:
    pending = _pending()
    if not pending:
        return
    by, client = origin.get()
    event = Event(tuple(sorted(pending)), by, client)
    pending.clear()
    try:
        send(event)
    except Exception:
        # Не удалось оповестить — страницы обновятся при следующем действии; правка сохранена.
        logger.exception("Не отправлено событие обновления")


def send(event: Event) -> None:
    with transaction.get_connection().cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, event.to_json()])


class Broker:
    """Одно соединение LISTEN на процесс; каждому подписчику — своя очередь."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[queue.Queue[Event]] = set()
        self._thread: threading.Thread | None = None

    def subscribe(self) -> "queue.Queue[Event]":
        box: queue.Queue[Event] = queue.Queue(maxsize=50)
        with self._lock:
            self._queues.add(box)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._listen, name="live", daemon=True)
                self._thread.start()
        return box

    def unsubscribe(self, box: "queue.Queue[Event]") -> None:
        with self._lock:
            self._queues.discard(box)

    def dispatch(self, event: Event) -> None:
        with self._lock:
            boxes = list(self._queues)
        for box in boxes:
            try:
                box.put_nowait(event)
            except queue.Full:
                # Вкладка не успевает читать — старое событие уже не важно.
                try:
                    box.get_nowait()
                    box.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def _listen(self) -> None:
        while True:
            with self._lock:
                if not self._queues:
                    self._thread = None
                    return
            try:
                params = connections["default"].get_connection_params()
                with psycopg.connect(**params, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    while True:
                        # Короткое ожидание: без подписчиков поток быстро закрывает соединение.
                        for notify in conn.notifies(timeout=1):
                            self.dispatch(_parse(notify.payload))
                        with self._lock:
                            if not self._queues:
                                self._thread = None
                                return
            except Exception:
                logger.exception("Соединение событий оборвалось — переподключение")
                time.sleep(2)


def _parse(payload: str) -> Event:
    try:
        data = json.loads(payload)
        return Event(tuple(data.get("topics", ())), data.get("by", ""), data.get("client", ""))
    except (ValueError, AttributeError):
        return Event(())


broker = Broker()
