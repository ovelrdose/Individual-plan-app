"""Экраны «Смены» и «Распорядок инструкторов» (TZ.md, FR-STF-4, FR-STF-5)."""

from datetime import date, time

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.staff.models import InstructorBlock, InstructorDuty, ShiftException, ShiftPattern

pytestmark = pytest.mark.django_db

HTMX = {"HTTP_HX_REQUEST": "true"}
MON = date(2026, 10, 5)


@pytest.fixture
def rehab_client(client, rehab):
    client.force_login(rehab)
    return client


@pytest.fixture
def volkov(instructors):
    item = instructors["Волков"]
    ShiftPattern.objects.create(
        instructor=item, pattern="5/2", anchor_date=MON, valid_from=date(2026, 9, 1)
    )
    return item


class TestAccess:
    URLS = ["staff:shifts", "staff:duties"]

    @pytest.mark.parametrize("name", URLS)
    def test_anonymous_redirected_to_login(self, client, name):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert reverse("login") in response.url

    @pytest.mark.parametrize("name", URLS)
    def test_doctor_forbidden(self, client, doctor, name):
        client.force_login(doctor)
        assert client.get(reverse(name)).status_code == 403

    def test_doctor_cannot_toggle(self, client, doctor, volkov):
        client.force_login(doctor)
        url = reverse("staff:shift_toggle", args=[volkov.pk])
        assert client.post(url, {"day": "2026-10-05"}, **HTMX).status_code == 403
        assert not ShiftException.objects.exists()

    @pytest.mark.parametrize("name", URLS)
    def test_admin_allowed(self, client, admin_user, name):
        client.force_login(admin_user)
        assert client.get(reverse(name)).status_code == 200

    def test_menu_only_for_rehab_and_admin(self, client, doctor, rehab):
        client.force_login(doctor)
        page = client.get(reverse("programs:list")).content.decode()
        assert reverse("staff:shifts") not in page
        client.force_login(rehab)
        page = client.get(reverse("programs:list")).content.decode()
        assert reverse("staff:shifts") in page and reverse("staff:duties") in page


class TestShifts:
    def test_month_page(self, rehab_client, volkov):
        response = rehab_client.get(reverse("staff:shifts"), {"month": "2026-10"})

        page = response.content.decode()
        assert response.status_code == 200
        assert "Октябрь 2026" in page
        assert 'id="shift-calendar"' in page
        # Пара 2/2 рядом: Волков, Лебедева, затем Соколов.
        assert page.index("Лебедева") < page.index("Соколов")
        assert len(response.context["rows"][0].cells) == 31

    def test_bad_month_falls_back_to_current(self, rehab_client, instructors):
        response = rehab_client.get(reverse("staff:shifts"), {"month": "2026-13"})
        assert response.status_code == 200

    def test_toggle_returns_fragment(self, rehab_client, volkov):
        url = reverse("staff:shift_toggle", args=[volkov.pk])

        response = rehab_client.post(url, {"day": "2026-10-05", "month": "2026-10"}, **HTMX)

        assert response.status_code == 200
        assert response.templates[0].name == "staff/_shift_calendar.html"
        assert "shift-exception" in response.content.decode()
        exception = ShiftException.objects.get()
        assert (exception.date, exception.is_working) == (MON, False)

        rehab_client.post(url, {"day": "2026-10-05", "month": "2026-10"}, **HTMX)
        assert not ShiftException.objects.exists()

    def test_toggle_bad_day(self, rehab_client, volkov):
        url = reverse("staff:shift_toggle", args=[volkov.pk])
        assert rehab_client.post(url, {"day": "вчера"}, **HTMX).status_code == 404

    def test_pattern_form_and_save(self, rehab_client, volkov):
        url = reverse("staff:shift_pattern", args=[volkov.pk])

        form = rehab_client.get(url, {"month": "2026-10"}, **HTMX)
        assert form.status_code == 200
        assert form.context["pattern_for"] == volkov

        response = rehab_client.post(
            url,
            {
                "month": "2026-10",
                "pattern-pattern": "2/2",
                "pattern-anchor_date": "2026-10-05",
                "pattern-valid_from": "2026-10-05",
            },
            **HTMX,
        )

        assert response.status_code == 200
        assert response.context.get("pattern_for") is None
        assert list(volkov.shift_patterns.values_list("pattern", "valid_to")) == [
            ("5/2", date(2026, 10, 4)),
            ("2/2", None),
        ]

    def test_pattern_error_in_fragment(self, rehab_client, volkov):
        url = reverse("staff:shift_pattern", args=[volkov.pk])
        data = {
            "month": "2026-10",
            "pattern-pattern": "2/2",
            "pattern-anchor_date": "2026-10-05",
            "pattern-valid_from": "2026-08-01",
        }

        response = rehab_client.post(url, data, **HTMX)

        assert response.status_code == 200
        assert "уже задан другой шаблон" in response.content.decode()
        assert volkov.shift_patterns.count() == 1


class TestDuties:
    def test_page_lists_duties_of_selected(self, rehab_client, instructors, slots):
        volkov, sokolov = instructors["Волков"], instructors["Соколов"]
        InstructorDuty.objects.create(
            instructor=sokolov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
        )

        first = rehab_client.get(reverse("staff:duties"))
        chosen = rehab_client.get(reverse("staff:duties"), {"instructor": sokolov.pk})

        assert first.context["selected"] == volkov
        assert "Распорядка нет" in first.content.decode()
        assert "Метод. работа" in chosen.content.decode()
        assert rehab_client.get(reverse("staff:duties"), {"instructor": 999999}).status_code == 404

    def test_add_group_lead(self, rehab_client, instructors, slots, ergo_session):
        volkov = instructors["Волков"]
        data = {
            "duty-slot": slots["9:10"].pk,
            "duty-kind": "GROUP_LEAD",
            "duty-group_session": ergo_session.pk,
            "duty-valid_from": "2026-10-05",
        }

        response = rehab_client.post(reverse("staff:duty_add", args=[volkov.pk]), data, **HTMX)

        assert response.status_code == 200
        assert response.templates[0].name == "staff/_duties.html"
        duty = InstructorDuty.objects.get()
        assert duty.group_session == ergo_session and duty.instructor == volkov
        assert "Эрго общая" in response.content.decode()

    def test_add_error_stays_in_fragment(self, rehab_client, instructors, slots):
        volkov = instructors["Волков"]
        data = {
            "duty-slot": slots["9:10"].pk,
            "duty-kind": "GROUP_LEAD",
            "duty-valid_from": "2026-10-05",
        }

        response = rehab_client.post(reverse("staff:duty_add", args=[volkov.pk]), data, **HTMX)

        assert response.status_code == 200
        assert "Выберите занятие группы" in response.content.decode()
        assert not InstructorDuty.objects.exists()

    def test_overlap_error(self, rehab_client, instructors, slots):
        volkov = instructors["Волков"]
        InstructorDuty.objects.create(
            instructor=volkov, slot=slots["15:00"], kind="METHOD_WORK", valid_from=MON
        )
        data = {"duty-slot": slots["15:00"].pk, "duty-kind": "BOS", "duty-valid_from": "2026-11-01"}

        response = rehab_client.post(reverse("staff:duty_add", args=[volkov.pk]), data, **HTMX)

        assert "уже есть «Метод. работа»" in response.content.decode()
        assert InstructorDuty.objects.count() == 1

    def test_edit_and_end(self, rehab_client, instructors, slots):
        duty = InstructorDuty.objects.create(
            instructor=instructors["Волков"], slot=slots["15:00"], kind="BOS", valid_from=MON
        )
        url = reverse("staff:duty_edit", args=[duty.pk])

        assert rehab_client.get(url, **HTMX).context["editing"] == duty
        rehab_client.post(
            url,
            {
                "edit-slot": slots["13:00"].pk,
                "edit-kind": "OTHER",
                "edit-label": "Обучение",
                "edit-valid_from": "2026-10-05",
            },
            **HTMX,
        )
        duty.refresh_from_db()
        assert (duty.slot, duty.label) == (slots["13:00"], "Обучение")

        end_url = reverse("staff:duty_end", args=[duty.pk])
        bad = rehab_client.post(end_url, {f"end{duty.pk}-valid_to": "2026-10-01"}, **HTMX)
        assert "раньше нельзя" in bad.content.decode()
        empty = rehab_client.post(end_url, {}, **HTMX)
        assert "Укажите последний день" in empty.content.decode()

        rehab_client.post(end_url, {f"end{duty.pk}-valid_to": "2026-10-20"}, **HTMX)
        duty.refresh_from_db()
        assert duty.valid_to == date(2026, 10, 20)

    def test_blocks_add_and_delete(self, rehab_client, instructors, slots):
        volkov = instructors["Волков"]
        url = reverse("staff:block_add", args=[volkov.pk])
        data = {"block-date": "2099-01-10", "block-slot": slots["9:10"].pk, "block-kind": "CLEAR"}

        response = rehab_client.post(url, data, **HTMX)

        assert response.status_code == 200
        assert response.templates[0].name == "staff/_blocks.html"
        assert "распорядок снят" in response.content.decode()
        block = InstructorBlock.objects.get()

        again = rehab_client.post(url, data, **HTMX)
        assert "уже есть разовый блок" in again.content.decode()

        deleted = rehab_client.post(reverse("staff:block_delete", args=[block.pk]), **HTMX)
        assert deleted.status_code == 200
        assert not InstructorBlock.objects.exists()

    def test_no_instructors(self, rehab_client):
        response = rehab_client.get(reverse("staff:duties"))
        assert "Действующих инструкторов нет" in response.content.decode()


class TestSeedDev:
    def test_shifts_and_duties(self, settings, full_catalog):
        settings.DEBUG = True

        call_command("seed_dev", password="x-pass-123")
        call_command("seed_dev", password="x-pass-123")

        assert ShiftPattern.objects.count() == 9
        lead = InstructorDuty.objects.filter(kind="GROUP_LEAD").order_by("slot__start")
        assert [(d.instructor.short_name, d.text) for d in lead] == [
            ("Орлова", "Эрго общая"),
            ("Орлова", "I can нога"),
        ]
        bos = InstructorDuty.objects.filter(kind="BOS").values_list("slot__start", flat=True)
        assert sorted(bos) == [time(13, 0), time(13, 40), time(14, 20)]
        method = InstructorDuty.objects.filter(kind="METHOD_WORK")
        assert method.filter(slot__start=time(15, 0)).count() == 2
        assert method.filter(slot__start=time(11, 10)).count() == 1
        assert InstructorDuty.objects.count() == 12

    def test_without_catalog_skips_group_lead(self, settings, capsys):
        settings.DEBUG = True

        call_command("seed_dev", password="x-pass-123")

        assert not InstructorDuty.objects.filter(kind="GROUP_LEAD").exists()
        assert InstructorDuty.objects.count() == 10
        assert "load_initial_catalog" in capsys.readouterr().out
