"""Раздел «Инструкторы» (решение 87): карточка с шаблоном смены в окне «Состава и смен»,
вкладки, права. Инструкторы вымышленные (``staff``): Волков/Лебедева 2/2, Соколов 5/2."""

import importlib
from datetime import date

import pytest
from django.apps import apps
from django.urls import reverse

from apps.accounts.models import Preset, Right, User
from apps.staff.models import Instructor, ShiftPattern

TUE = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr("apps.staff.views.timezone.localdate", lambda: TUE)


def htmx(client, url: str, data: dict | None = None) -> str:
    response = client.post(url, data or {}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response.content.decode()


def card(short_name: str, **changes) -> dict:
    data = {
        "ins-short_name": short_name,
        "ins-full_name": f"{short_name} Тест Тестович",
        "ins-partner": "",
        "ins-display_order": "5",
        "ins-pattern": "",
        "ins-anchor_date": "",
        "ins-valid_from": "",
        "month": "2026-10",
    }
    return data | {f"ins-{key}": value for key, value in changes.items()}


class TestCard:
    def test_new_instructor_with_pattern(self, client, rehab, staff):
        client.force_login(rehab)
        html = htmx(
            client,
            reverse("staff:instructor_add"),
            card("Орлов", pattern="5/2", anchor_date="2026-10-05", valid_from="2026-10-07"),
        )
        orlov = Instructor.objects.get(short_name="Орлов")
        [pattern] = orlov.shift_patterns.all()
        assert (pattern.pattern, pattern.valid_from) == ("5/2", date(2026, 10, 7))
        assert "заведён" in html and "Шаблон смены не задан" not in html
        # Календарь обновился в том же ответе и показывает нового инструктора.
        assert 'id="shift-calendar" data-panel-table hx-swap-oob="true"' in html
        assert "Орлов" in html.split('id="shift-calendar"')[1]

    def test_new_without_pattern_warns(self, client, rehab, staff):
        client.force_login(rehab)
        html = htmx(client, reverse("staff:instructor_add"), card("Орлов"))
        assert "Шаблон смены не задан" in html
        assert not ShiftPattern.objects.filter(instructor__short_name="Орлов").exists()

    def test_pattern_without_dates_is_error(self, client, rehab, staff):
        client.force_login(rehab)
        html = htmx(client, reverse("staff:instructor_add"), card("Орлов", pattern="2/2"))
        assert "Укажите дату." in html and not Instructor.objects.filter(short_name="Орлов")

    def test_edit_card_keeps_pattern(self, client, rehab, staff):
        """Сохранили карточку, не трогая шаблон, — нового периода смены нет."""
        client.force_login(rehab)
        sokolov = staff["sokolov"]
        htmx(
            client,
            reverse("staff:instructor_edit", args=[sokolov.pk]),
            card("Соколов", pattern="5/2", anchor_date="2026-10-05", valid_from="2026-10-06"),
        )
        assert sokolov.shift_patterns.count() == 1

    def test_edit_changes_pattern_from_date(self, client, rehab, staff):
        client.force_login(rehab)
        sokolov = staff["sokolov"]
        html = htmx(
            client,
            reverse("staff:instructor_edit", args=[sokolov.pk]),
            card("Соколов", pattern="daily", anchor_date="2026-10-05", valid_from="2026-10-12"),
        )
        old, new = sokolov.shift_patterns.order_by("valid_from")
        assert old.valid_to == date(2026, 10, 11) and new.pattern == "daily"
        assert "Сохранено." in html and "каждый день с 12.10.2026" in html

    def test_pattern_cannot_be_cleared(self, client, rehab, staff):
        client.force_login(rehab)
        sokolov = staff["sokolov"]
        html = htmx(client, reverse("staff:instructor_edit", args=[sokolov.pk]), card("Соколов"))
        assert "Шаблон не убирают" in html and sokolov.shift_patterns.count() == 1

    def test_pattern_error_after_card_saved(self, client, rehab, staff):
        client.force_login(rehab)
        sokolov = staff["sokolov"]
        ShiftPattern.objects.filter(instructor=sokolov).update(valid_to=date(2026, 11, 1))
        ShiftPattern.objects.create(
            instructor=sokolov, pattern="daily", anchor_date=TUE, valid_from=date(2026, 11, 2)
        )
        html = htmx(
            client,
            reverse("staff:instructor_edit", args=[sokolov.pk]),
            card(
                "Соколов",
                full_name="Соколов Новое Имя",
                pattern="2/2",
                **{
                    "anchor_date": "2026-10-05",
                    "valid_from": "2026-10-12",
                },
            ),
        )
        assert "Карточка сохранена, а шаблон смены — нет: С 02.11.2026" in html
        sokolov.refresh_from_db()
        assert sokolov.full_name == "Соколов Новое Имя"

    def test_archived_listed_with_card(self, client, rehab, staff):
        volkov = staff["volkov"]
        Instructor.objects.filter(pk=volkov.pk).update(is_active=False)
        client.force_login(rehab)
        page = client.get(reverse("staff:shifts")).content.decode()
        assert "В архиве (1)" in page
        assert reverse("staff:instructor_edit", args=[volkov.pk]) in page


class TestSection:
    def test_tabs_and_single_menu_item(self, client, rehab, staff):
        client.force_login(rehab)
        for url in (reverse("staff:shifts"), reverse("staff:duties"), reverse("scheduling:load")):
            page = client.get(url).content.decode()
            assert "Состав и смены" in page and "Распорядок" in page and "Загрузка" in page
        assert "dropdown-toggle" not in page.split("Инструкторы")[0][-400:]

    def test_catalog_has_no_instructors(self, client, rehab, staff):
        client.force_login(rehab)
        page = client.get(reverse("catalog:index")).content.decode()
        assert "?tab=instructors" not in page
        response = client.get(reverse("catalog:index"), {"tab": "instructors"})
        assert response.status_code == 302 and response["Location"] == reverse("staff:shifts")

    def test_card_needs_staff_right(self, client, make_user, department, staff):
        catalog_only = make_user("cat", rights=[Right.CATALOG.value])
        client.force_login(catalog_only)
        assert client.post(reverse("staff:instructor_add"), card("Орлов")).status_code == 403
        staff_only = make_user("stf", rights=[Right.STAFF.value])
        client.force_login(staff_only)
        assert "заведён" in htmx(client, reverse("staff:instructor_add"), card("Орлов"))

    def test_rehab_preset_keeps_access(self, make_user, department):
        user = make_user("r", (department, Preset.REHAB))
        assert Right.STAFF.value in user.rights


@pytest.mark.django_db
def test_migration_grants_staff_to_catalog_editors():
    """Кто правил инструкторов по праву «Справочники», не теряет доступ."""
    catalog = User.objects.create_user("cat", rights=["catalog"])
    both = User.objects.create_user("both", rights=["catalog", "staff"])
    other = User.objects.create_user("doc", rights=["programs_edit"])
    migration = importlib.import_module("apps.accounts.migrations.0005_instructors_right")

    migration.grant_staff(apps, None)

    rights = {u.username: u.rights for u in User.objects.order_by("username")}
    assert rights == {
        "both": ["catalog", "staff"],
        "cat": ["catalog", "staff"],
        "doc": ["programs_edit"],
    }
    assert catalog.pk and both.pk and other.pk


def test_archived_card_saves_without_pattern(client, rehab, staff):
    """У архивного полей смены нет — правка ФИО не спотыкается о закончившийся шаблон."""
    from apps.scheduling import staff_changes

    sokolov = staff["sokolov"]
    staff_changes.delete_instructor(rehab, sokolov)
    client.force_login(rehab)
    html = htmx(
        client,
        reverse("staff:instructor_edit", args=[sokolov.pk]),
        card("Соколов", full_name="Соколов Другой"),
    )
    assert "Сохранено." in html
