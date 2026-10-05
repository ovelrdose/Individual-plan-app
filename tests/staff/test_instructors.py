import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.staff.models import Instructor
from apps.staff.services import set_partner

pytestmark = pytest.mark.django_db


@pytest.fixture
def team():
    names = ["Соколов", "Морозова", "Волков", "Лебедева"]
    return [
        Instructor.objects.create(short_name=name, full_name=name, display_order=order)
        for order, name in enumerate(names)
    ]


def partners():
    return dict(Instructor.objects.values_list("short_name", "partner__short_name"))


def test_set_partner_is_symmetric(team):
    a, b, *_ = team

    set_partner(a, b)

    assert partners()["Соколов"] == "Морозова"
    assert partners()["Морозова"] == "Соколов"
    assert a.partner == b and b.partner == a


def test_new_pair_breaks_old_pairs(team):
    a, b, c, d = team
    set_partner(a, b)
    set_partner(c, d)

    set_partner(a, c)

    assert partners() == {
        "Соколов": "Волков",
        "Волков": "Соколов",
        "Морозова": None,
        "Лебедева": None,
    }


def test_unset_partner(team):
    a, b, *_ = team
    set_partner(a, b)

    set_partner(b, None)

    assert partners()["Соколов"] is None
    assert partners()["Морозова"] is None


def test_self_partner_is_rejected(team):
    with pytest.raises(ValueError):
        set_partner(team[0], team[0])
    team[0].partner = team[0]
    with pytest.raises(ValidationError):
        team[0].full_clean()


def test_changes_are_logged(team):
    a, b, *_ = team

    set_partner(a, b)

    assert a.history.first().partner_id == b.pk
    assert b.history.first().partner_id == a.pk


def test_team_label(team):
    a, b, *_ = team
    assert a.team_label == "Соколов"

    set_partner(b, a)

    assert b.team_label == a.team_label == "Соколов/ Морозова"


def test_admin_keeps_pair_symmetric(client, admin_user, team):
    a, b, c, _ = team
    set_partner(a, b)
    client.force_login(admin_user)
    url = reverse("admin:staff_instructor_change", args=[c.pk])

    response = client.post(
        url,
        {
            "full_name": c.full_name,
            "short_name": c.short_name,
            "partner": a.pk,
            "display_order": c.display_order,
            "is_active": "on",
        },
    )

    assert response.status_code == 302
    assert partners() == {
        "Соколов": "Волков",
        "Волков": "Соколов",
        "Морозова": None,
        "Лебедева": None,
    }


def test_admin_partner_choices_exclude_self(client, admin_user, team):
    client.force_login(admin_user)

    response = client.get(reverse("admin:staff_instructor_change", args=[team[0].pk]))

    choices = response.context["adminform"].form.fields["partner"].queryset
    assert team[0] not in choices
    assert team[1] in choices
