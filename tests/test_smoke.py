import pytest
from django.db import connection
from django.urls import reverse


@pytest.mark.django_db
def test_database_is_postgresql():
    assert connection.vendor == "postgresql"
    assert connection.pg_version >= 160000


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
