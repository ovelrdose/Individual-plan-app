import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.urls import reverse


@pytest.mark.django_db
def test_database_is_postgresql():
    assert connection.vendor == "postgresql"
    assert connection.pg_version >= 160000


def test_database_pool_checks_connections():
    """Пул соединений с проверкой перед выдачей: оборванное после перезапуска PostgreSQL
    соединение заменяется новым (docs/deploy.md, DB_POOL_MAX_SIZE)."""
    database = settings.DATABASES["default"]
    assert database["OPTIONS"]["pool"]["max_size"] >= 1
    assert database["CONN_HEALTH_CHECKS"] is True
    assert database.get("CONN_MAX_AGE", 0) == 0  # с пулом постоянные соединения запрещены


@pytest.mark.django_db(transaction=True)
def test_pool_replaces_broken_connections():
    """Перезапуск PostgreSQL обрывает соединения, ждущие в пуле, — запрос их не получает."""
    connection.ensure_connection()
    connection.close()  # с пулом соединение не закрывается, а возвращается в пул
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


def test_home_page(client, doctor):
    client.force_login(doctor)
    response = client.get("/", follow=True)
    assert response.status_code == 200
    assert "Программа реабилитации" in response.content.decode()


def test_scripts_only_in_head(client, rehab):
    """С hx-boost htmx вставлял бы скрипты новой страницы в body и выполнял их снова — меню
    Bootstrap открывалось и тут же закрывалось. Скрипты — только в head, выполнение из ответов
    выключено."""
    client.force_login(rehab)
    page = client.get(reverse("scheduling:board")).content.decode()
    head, body = page.split("<body", 1)
    assert '"allowScriptTags": false' in head and "js/board.js" in head
    assert "<script" not in body
