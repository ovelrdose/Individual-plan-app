"""Выбор группы бассейна специалистом ФР (TZ.md, FR-PRG-3, шаг 2.2)."""

from datetime import time

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.catalog.models import GroupSession, Procedure
from apps.programs import services as programs
from apps.programs.models import Prescription
from apps.scheduling.services import ScheduleError, choose_pool_group, program_schedule

pytestmark = pytest.mark.usefixtures("full_catalog")


def proc(name: str) -> Procedure:
    return Procedure.objects.get(name=name, department=None)


def prescribe(user, program, name: str, **fields) -> Prescription:
    return programs.add_prescription(
        user, Prescription(program=program, procedure=proc(name), **fields)
    )


def codes(program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


@pytest.fixture
def program(make_program):
    return make_program(shrm=4)  # 05.10–19.10, 15 дней


@pytest.fixture
def pool(doctor, program):
    return prescribe(doctor, program, "Бассейн")


def test_generic_pool_waits_for_group(program, pool):
    assert codes(program) == ["POOL_TYPE_REQUIRED"]
    assert not program.bookings.exists()


def test_rehab_chooses_group_and_schedule_follows(rehab, program, pool):
    choose_pool_group(rehab, pool, proc("ЛФК в воде: спина"))

    pool.refresh_from_db()
    assert pool.procedure.name == "Бассейн", "запись врача не меняется"
    assert pool.pool_group.name == "ЛФК в воде: спина"
    assert codes(program) == []
    bookings = program.bookings.filter(prescription=pool)
    assert bookings.count() == 15
    assert {b.procedure.name for b in bookings} == {"ЛФК в воде: спина"}
    assert {b.start for b in bookings} == {time(10, 30)}
    assert pool.history.first().history_user == rehab


def test_group_time_respects_other_sessions(doctor, rehab, program, pool):
    # «Спина» в 10:30 занята группой I can нога — бассейн уходит на следующее занятие спины.
    prescribe(doctor, program, "I can нога")

    choose_pool_group(rehab, pool, proc("ЛФК в воде: спина"))

    assert {b.start for b in program.bookings.filter(prescription=pool)} == {time(13, 30)}


def test_change_and_reset_group(rehab, program, pool):
    choose_pool_group(rehab, pool, proc("ЛФК в воде: спина"))
    choose_pool_group(rehab, pool, proc("ЛФК в воде: верхняя конечность"))

    assert {b.start for b in program.bookings.filter(prescription=pool)} == {time(11, 15)}

    choose_pool_group(rehab, pool, None)

    assert not program.bookings.filter(prescription=pool).exists()
    assert codes(program) == ["POOL_TYPE_REQUIRED"]


def test_group_survives_doctor_edit(doctor, rehab, program, pool):
    choose_pool_group(rehab, pool, proc("ЛФК в воде: спина"))
    pool.duration_min = 30

    programs.update_prescription(doctor, pool)

    assert program.bookings.filter(prescription=pool).count() == 15


def test_doctor_cannot_choose(doctor, pool):
    with pytest.raises(PermissionDenied):
        choose_pool_group(doctor, pool, proc("ЛФК в воде: спина"))


def test_admin_can_choose(admin_user, pool):
    choose_pool_group(admin_user, pool, proc("ЛФК в воде: спина"))


def test_only_for_generic_pool(doctor, rehab, program):
    item = prescribe(doctor, program, "ЛФК в воде: спина")

    with pytest.raises(ScheduleError, match="только для назначения «Бассейн»"):
        choose_pool_group(rehab, item, proc("ЛФК в воде: верхняя конечность"))


def test_group_must_be_a_pool_group_with_sessions(rehab, pool):
    with pytest.raises(ScheduleError, match="действующую группу"):
        choose_pool_group(rehab, pool, proc("Эрго общая"))
    with pytest.raises(ScheduleError, match="действующую группу"):
        choose_pool_group(rehab, pool, proc("Бассейн"))

    group = proc("ЛФК в воде: верхняя конечность")
    GroupSession.objects.filter(procedure=group).update(is_active=False)
    with pytest.raises(ScheduleError, match="действующую группу"):
        choose_pool_group(rehab, pool, group)


def test_schedule_lists_pool_choices(pool, program):
    schedule = program_schedule(program)

    assert schedule.pools == [pool]
    options = {o.procedure.name: o.times for o in schedule.pool_groups}
    assert options["ЛФК в воде: нижняя конечность"] == "9:00, 9:45, 14:15, 15:40"
    assert "Бассейн" not in options


class TestScreen:
    def url(self, program, pool):
        return reverse("scheduling:program_pool_group", args=[program.pk, pool.pk])

    def test_rehab_sees_selector_doctor_does_not(self, client, doctor, rehab, program, pool):
        page = reverse("programs:detail", args=[program.pk])

        client.force_login(rehab)
        assert "— не выбрана —" in client.get(page).content.decode()

        client.force_login(doctor)
        html = client.get(page).content.decode()
        assert "— не выбрана —" not in html
        assert "не выбрана — выбирает специалист ФР" in html

    def test_htmx_choice_returns_schedule_block(self, client, rehab, program, pool):
        client.force_login(rehab)
        group = proc("ЛФК в воде: спина")

        response = client.post(
            self.url(program, pool), {"group": group.pk}, headers={"HX-Request": "true"}
        )

        html = response.content.decode()
        assert response.status_code == 200
        assert html.lstrip().startswith('<div id="schedule"')
        assert f'value="{group.pk}" selected' in html
        assert "Типичный день" in html

    def test_error_is_shown_in_block(self, client, rehab, program, pool):
        client.force_login(rehab)

        response = client.post(
            self.url(program, pool),
            {"group": proc("Эрго общая").pk},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "Выберите действующую группу бассейна" in response.content.decode()

    def test_plain_post_redirects(self, client, rehab, program, pool):
        client.force_login(rehab)

        response = client.post(self.url(program, pool), {"group": proc("ЛФК в воде: спина").pk})

        assert response.status_code == 302
        pool.refresh_from_db()
        assert pool.pool_group is not None

    def test_doctor_gets_403(self, client, doctor, program, pool):
        client.force_login(doctor)

        response = client.post(self.url(program, pool), {"group": proc("ЛФК в воде: спина").pk})

        assert response.status_code == 403

    def test_other_department_gets_404(self, client, make_user, other_department, program, pool):
        from apps.accounts.models import Role

        client.force_login(make_user("rehab2", (other_department, Role.REHAB)))

        response = client.post(self.url(program, pool), {"group": proc("ЛФК в воде: спина").pk})

        assert response.status_code == 404
