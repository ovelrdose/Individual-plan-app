import pytest
from django.db import connection


@pytest.mark.django_db
def test_database_is_postgresql():
    assert connection.vendor == "postgresql"
    assert connection.pg_version >= 160000


def test_home_page(client, doctor):
    client.force_login(doctor)
    response = client.get("/", follow=True)
    assert response.status_code == 200
    assert "Программа реабилитации" in response.content.decode()
