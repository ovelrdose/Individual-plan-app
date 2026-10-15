"""gunicorn на проде (docs/deploy.md). Запуск — из systemd: deploy/omr4.service.

Поток обновлений /events/ (SSE, TZ.md NFR-11) занимает поток, пока открыта вкладка, поэтому
воркеры — с потоками и с запасом: 3 × 64 = 192 одновременных запроса на ~50 пользователей
с несколькими вкладками. Соединение с базой поток не держит (apps/live/views.stream).
"""

bind = "127.0.0.1:8000"  # снаружи — только через nginx
worker_class = "gthread"
workers = 3
threads = 64
timeout = 60
# Потоки событий бесконечны: при перезапуске ждём недолго, браузер переподключится сам.
graceful_timeout = 10
keepalive = 5
# Управляющий сокет gunicorn не нужен: перезапуск — через systemctl (ProtectSystem=strict).
control_socket_disable = True

# Журнал доступа выключен: в адресах бывает поиск по ФИО (NFR-7). Ошибки — в stderr → journald.
accesslog = None
errorlog = "-"
loglevel = "info"
