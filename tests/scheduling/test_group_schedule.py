"""Экран «Расписание групп» (TZ.md, FR-SCH-18, шаг 2.3)."""

from datetime import date, time, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.accounts.models import Role
from apps.catalog.models import Equipment, GroupSession, Procedure, ProcedureKind
from apps.programs import services as programs
from apps.programs.models import Prescription
from apps.scheduling.models import Booking
from apps.scheduling.services import save_equipment, save_group_session

pytestmark = pytest.mark.usefixtures("full_catalog")

TODAY = date(2026, 10, 5)  # первый день курса программ из make_program


def proc(name: str) -> Procedure:
    return Procedure.objects.get(name=name, department=None)


def session(name: str, start: time) -> GroupSession:
    return GroupSession.objects.get(procedure=proc(name), start_time=start)


def prescribe(user, program, name: str, **fields) -> Prescription:
    return programs.add_prescription(
        user, Prescription(program=program, procedure=proc(name), **fields)
    )


def starts(program, name: str) -> set[time]:
    return {b.start for b in program.bookings.filter(procedure__name=name)}


def codes(program) -> list[str]:
    program.refresh_from_db()
    return [issue["code"] for issue in program.schedule_issues]


@pytest.fixture
def program(make_program):
    return make_program(shrm=4)  # 05.10–19.10


class TestGroupSession:
    def test_time_change_moves_current_programs(self, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        item.start_time = time(9, 0)

        result = save_group_session(rehab, item, today=TODAY)

        assert result.programs == [program]
        assert starts(program, "Эрго общая") == {time(9, 0)}
        assert item.history.first().history_user == rehab

    def test_finished_programs_are_not_touched(self, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        item.start_time = time(9, 0)

        result = save_group_session(rehab, item, today=date(2026, 10, 20))

        assert result.programs == []
        assert starts(program, "Эрго общая") == {time(9, 10)}

    def test_other_department_programs_follow_too(self, make_user, other_department, rehab):
        # Группы — ресурс центра: время меняется для пациентов всех отделений.
        from apps.programs.models import Program

        other_doctor = make_user("doctor2", (other_department, Role.DOCTOR))
        other = programs.save_program(
            other_doctor,
            Program(
                department=other_department, full_name="Другов Друг Другович", room="3",
                shrm=4, attending_doctor=other_doctor, start_date=TODAY,
            ),
            end_date_changed=False,
        )  # fmt: skip
        prescribe(other_doctor, other, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        item.start_time = time(9, 0)

        result = save_group_session(rehab, item, today=TODAY)

        assert result.programs == [other]
        assert starts(other, "Эрго общая") == {time(9, 0)}

    def test_pool_group_chosen_by_rehab_follows(self, doctor, rehab, program):
        from apps.scheduling.services import choose_pool_group

        pool = prescribe(doctor, program, "Бассейн")
        choose_pool_group(rehab, pool, proc("ЛФК в воде: верхняя конечность"))
        item = session("ЛФК в воде: верхняя конечность", time(11, 15))
        item.start_time = time(16, 20)

        result = save_group_session(rehab, item, today=TODAY)

        assert result.programs == [program]
        assert {b.start for b in program.bookings.filter(prescription=pool)} == {time(16, 20)}

    def test_pinned_at_old_time_is_listed_not_moved(self, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        booking = program.bookings.get(date=date(2026, 10, 6))
        Booking.objects.filter(pk=booking.pk).update(pinned=True)
        item = session("Эрго общая", time(9, 10))
        item.start_time = time(9, 0)

        result = save_group_session(rehab, item, today=TODAY)

        assert [b.pk for b in result.pinned] == [booking.pk]
        assert Booking.objects.get(pk=booking.pk).start == time(9, 10)

    def test_new_session_resolves_frequency_conflict(self, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая", per_day=2)
        assert codes(program) == ["GROUP_FREQUENCY"]

        save_group_session(
            rehab, GroupSession(procedure=proc("Эрго общая"), start_time=time(16, 20)), today=TODAY
        )

        assert codes(program) == []
        assert starts(program, "Эрго общая") == {time(9, 10), time(16, 20)}

    def test_deactivated_session_leaves_conflict(self, doctor, rehab, program):
        prescribe(doctor, program, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        item.is_active = False

        save_group_session(rehab, item, today=TODAY)

        assert not program.bookings.exists()
        assert codes(program) == ["NO_SESSIONS"]

    def test_duplicate_time_is_rejected(self, rehab):
        duplicate = GroupSession(procedure=proc("Эрго общая"), start_time=time(9, 10))

        with pytest.raises(ValidationError):
            save_group_session(rehab, duplicate, today=TODAY)

    def test_doctor_cannot_edit(self, doctor):
        with pytest.raises(PermissionDenied):
            save_group_session(doctor, session("Эрго общая", time(9, 10)), today=TODAY)

    def test_department_group_only_for_its_rehab(self, rehab, other_department):
        group = Procedure.objects.create(
            name="Своя группа", card_label="Своя", kind=ProcedureKind.LFK_GROUP,
            department=other_department,
        )  # fmt: skip

        with pytest.raises(PermissionDenied):
            save_group_session(rehab, GroupSession(procedure=group, start_time=time(8)))


class TestEquipment:
    def test_capacity_change_replans(self, doctor, rehab, program, make_program):
        prescribe(doctor, program, "st-150")
        second = make_program(full_name="Второв Второй Вторович", room="5а")
        prescribe(doctor, second, "st-150")
        assert starts(second, "st-150") == {time(13, 15)}
        st = Equipment.objects.get(name="st-150")
        st.capacity = 2

        result = save_equipment(rehab, st, today=TODAY)

        assert result.programs == [program, second]
        assert st.history.first().history_user == rehab

    def test_window_that_fits_nothing_is_rejected(self, rehab):
        st = Equipment.objects.get(name="st-150")
        st.window_end = time(13, 10)

        with pytest.raises(ValidationError):
            save_equipment(rehab, st, today=TODAY)

    def test_doctor_cannot_edit(self, doctor):
        with pytest.raises(PermissionDenied):
            save_equipment(doctor, Equipment.objects.get(name="st-150"), today=TODAY)


class TestScreen:
    url = reverse("scheduling:group_schedule")

    def test_rehab_sees_page_and_menu(self, client, rehab):
        client.force_login(rehab)

        html = client.get(self.url).content.decode()

        assert "Расписание групп" in html
        assert "Эрго общая" in html and "st-150" in html
        assert 'href="/schedule/groups/"' in html

    def test_doctor_has_no_access(self, client, doctor):
        client.force_login(doctor)

        assert client.get(self.url).status_code == 403
        menu = client.get(reverse("programs:list")).content.decode()
        assert 'href="/schedule/groups/"' not in menu

    def test_edit_session_shows_rescheduled_programs(self, client, doctor, rehab, make_program):
        current = make_program(start_date=date.today() - timedelta(days=1))
        prescribe(doctor, current, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        client.force_login(rehab)

        form = client.get(reverse("scheduling:session_edit", args=[item.pk]))
        assert 'value="09:10"' in form.content.decode()

        response = client.post(
            reverse("scheduling:session_edit", args=[item.pk]),
            {
                "edit-procedure": item.procedure_id,
                "edit-start_time": "09:00",
                "edit-duration_min": 30,
                "edit-place": "",
                "edit-is_active": "on",
            },
        )

        html = response.content.decode()
        assert html.lstrip().startswith('<div id="group-sessions"')
        assert "Расписание пересобрано у программ (1): 9п Тестов" in html
        assert starts(current, "Эрго общая") == {time(9, 0)}

    def test_add_duplicate_shows_form_error(self, client, rehab):
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:session_add"),
            {"procedure": proc("Эрго общая").pk, "start_time": "09:10", "duration_min": 30},
        )

        assert response.status_code == 200
        assert "уже существует" in response.content.decode()
        assert GroupSession.objects.filter(procedure=proc("Эрго общая")).count() == 1

    def test_equipment_model_error_is_shown(self, client, rehab):
        st = Equipment.objects.get(name="st-150")
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:equipment_edit", args=[st.pk]),
            {
                "eq-window_start": "13:00",
                "eq-window_end": "13:10",
                "eq-step_min": 15,
                "eq-duration_min": 15,
                "eq-capacity": 1,
                "eq-is_active": "on",
            },
        )

        assert response.status_code == 200
        assert "не помещается ни одной записи" in response.content.decode()
        st.refresh_from_db()
        assert st.window_end == time(15, 0)

    def test_doctor_cannot_post(self, client, doctor):
        client.force_login(doctor)

        response = client.post(
            reverse("scheduling:session_add"),
            {"procedure": proc("Эрго общая").pk, "start_time": "16:20", "duration_min": 30},
        )

        assert response.status_code == 403


class TestAdmin:
    """Расписание групп правится и в справочниках — программы должны следовать и туда."""

    def test_session_change_in_admin_replans(self, client, admin_user, doctor, make_program):
        current = make_program(start_date=date.today() - timedelta(days=1))
        prescribe(doctor, current, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_groupsession_change", args=[item.pk]),
            {
                "procedure": item.procedure_id,
                "start_time": "09:00",
                "duration_min": 30,
                "place": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        assert starts(current, "Эрго общая") == {time(9, 0)}

    def test_equipment_change_in_admin_replans(self, client, admin_user, doctor, make_program):
        current = make_program(start_date=date.today() - timedelta(days=1))
        prescribe(doctor, current, "st-150")
        st = Equipment.objects.get(name="st-150")
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_equipment_change", args=[st.pk]),
            {
                "name": "st-150",
                "window_start": "14:00",
                "window_end": "15:00",
                "step_min": 15,
                "duration_min": 15,
                "capacity": 1,
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        assert starts(current, "st-150") == {time(14, 0)}


class TestReviewFixes:
    def test_generic_pool_has_no_schedule(self, rehab):
        generic = proc("Бассейн")

        assert generic.is_generic_pool
        with pytest.raises(ValidationError, match="нет расписания"):
            save_group_session(rehab, GroupSession(procedure=generic, start_time=time(8)))
        from apps.scheduling.services import group_schedule_procedures

        assert generic not in group_schedule_procedures(rehab)

    def test_group_choice_only_for_pool_without_sessions(self):
        group = proc("Эрго общая")
        group.group_choice = True
        with pytest.raises(ValidationError, match="только у бассейна"):
            group.full_clean()

        pool_group = proc("ЛФК в воде: спина")
        pool_group.group_choice = True
        with pytest.raises(ValidationError, match="уже группа бассейна"):
            pool_group.full_clean()

    def test_session_cannot_move_to_other_group(self, rehab):
        item = session("Эрго общая", time(9, 10))
        item.procedure = proc("I can нога")

        from apps.scheduling.services import ScheduleError

        with pytest.raises(ScheduleError, match="Группу у занятия не меняют"):
            save_group_session(rehab, item, today=TODAY)

    def test_screen_does_not_change_group(self, client, rehab):
        item = session("Эрго общая", time(9, 10))
        client.force_login(rehab)

        client.post(
            reverse("scheduling:session_edit", args=[item.pk]),
            {
                "edit-procedure": proc("I can нога").pk,
                "edit-start_time": "09:10",
                "edit-duration_min": 30,
                "edit-is_active": "on",
            },
        )

        item.refresh_from_db()
        assert item.procedure.name == "Эрго общая"

    def test_admin_move_replans_both_groups(self, client, admin_user, doctor, make_program):
        current = make_program(start_date=date.today() - timedelta(days=1))
        prescribe(doctor, current, "Эрго общая")
        item = session("Эрго общая", time(9, 10))
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:catalog_groupsession_change", args=[item.pk]),
            {
                "procedure": proc("I can нога").pk,
                "start_time": "09:10",
                "duration_min": 30,
                "place": "",
                "is_active": "on",
            },
        )

        assert response.status_code == 302
        # У Эрго общей больше нет занятий: пациент не остаётся в «чужой» группе.
        assert not current.bookings.filter(procedure__name="Эрго общая").exists()
        assert codes(current) == ["NO_SESSIONS"]

    def test_garbage_pool_group_value_is_not_500(self, client, doctor, rehab, program):
        pool = prescribe(doctor, program, "Бассейн")
        client.force_login(rehab)

        response = client.post(
            reverse("scheduling:program_pool_group", args=[program.pk, pool.pk]),
            {"group": "abc"},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
