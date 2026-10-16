import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory

from apps.accounts import access
from apps.accounts.models import Preset, Right


def request_for(user, session_department=None):
    request = RequestFactory().get("/")
    request.user = user
    request.session = SessionStore()
    if session_department is not None:
        request.session[access.SESSION_DEPARTMENT_KEY] = session_department.pk
    return request


class TestDepartmentsFor:
    def test_member_sees_only_own_departments(self, doctor, department, other_department):
        assert list(access.departments_for(doctor)) == [department]

    def test_admin_sees_all_active(self, admin_user, department, other_department):
        other_department.is_active = False
        other_department.save()

        assert list(access.departments_for(admin_user)) == [department]

    def test_inactive_department_is_hidden(self, doctor, department):
        department.is_active = False
        department.save()

        assert not access.departments_for(doctor).exists()

    def test_inactive_user_and_anonymous_see_nothing(self, doctor):
        doctor.is_active = False
        doctor.save()

        assert not access.departments_for(doctor).exists()
        assert not access.departments_for(AnonymousUser()).exists()

    def test_several_memberships(self, make_user, department, other_department):
        user = make_user("both", (department, Preset.DOCTOR), (other_department, Preset.REHAB))

        assert set(access.departments_for(user)) == {department, other_department}

    def test_users_right_sees_all(self, make_user, department, other_department):
        user = make_user("boss", (department, Preset.DOCTOR), rights=[Right.USERS])

        assert set(access.departments_for(user)) == {department, other_department}
        assert access.is_admin(user)


class TestRights:
    """Права общие для всех отделений пользователя (решение 79)."""

    @pytest.mark.parametrize(
        ("user_fixture", "right", "expected"),
        [
            ("doctor", Right.PROGRAMS_EDIT, True),
            ("doctor", Right.PROGRAMS_DELETE, False),
            ("doctor", Right.SCHEDULE, False),
            ("rehab", Right.PROGRAMS_DELETE, True),
            ("rehab", Right.SCHEDULE, True),
            ("rehab", Right.USERS, False),
            ("admin_user", Right.USERS, True),
            ("admin_user", Right.SCHEDULE, True),
        ],
    )
    def test_has_right(self, request, department, user_fixture, right, expected):
        user = request.getfixturevalue(user_fixture)

        assert access.has_right(user, right, department) is expected
        assert access.has_right(user, right) is expected

    def test_only_in_own_departments(self, rehab, other_department):
        assert access.has_right(rehab, Right.SCHEDULE)
        assert not access.has_right(rehab, Right.SCHEDULE, other_department)

    def test_inactive_department_or_user_denies(self, rehab, department):
        department.is_active = False
        department.save()

        assert not access.has_right(rehab, Right.SCHEDULE, department)
        rehab.is_active = False
        assert not access.has_right(rehab, Right.SCHEDULE)
        assert not access.has_right(AnonymousUser(), Right.SCHEDULE)

    def test_require_right(self, doctor, department):
        access.require_right(doctor, Right.PROGRAMS_EDIT, department)
        with pytest.raises(PermissionDenied):
            access.require_right(doctor, Right.SCHEDULE, department)

    def test_departments_with(self, doctor, rehab, department):
        assert list(access.departments_with(rehab, Right.SCHEDULE)) == [department]
        assert not access.departments_with(doctor, Right.SCHEDULE).exists()


class TestAttendingDoctor:
    def test_flag_and_department(self, doctor, rehab, department, other_department):
        assert access.is_attending_doctor(doctor, department)
        assert not access.is_attending_doctor(doctor, other_department)
        assert not access.is_attending_doctor(rehab, department)

    def test_doctors_for(self, doctor, rehab, make_user, department):
        make_user("gone", (department, Preset.DOCTOR), is_active=False)

        assert list(access.doctors_for(department)) == [doctor]


class TestCurrentDepartment:
    def test_foreign_department_is_404(self, doctor, other_department):
        with pytest.raises(Http404):
            access.get_department_or_404(doctor, other_department.pk)

    def test_garbage_id_is_404(self, doctor):
        with pytest.raises(Http404):
            access.get_department_or_404(doctor, "abc")

    def test_defaults_to_first_available(self, doctor, department):
        request = request_for(doctor)

        assert access.current_department(request) == department
        assert request.session[access.SESSION_DEPARTMENT_KEY] == department.pk

    def test_stale_selection_falls_back(self, doctor, department, other_department):
        request = request_for(doctor, session_department=other_department)

        assert access.current_department(request) == department

    def test_selected_department_is_kept(self, make_user, department, other_department):
        user = make_user("both", (department, Preset.DOCTOR), (other_department, Preset.DOCTOR))
        request = request_for(user)

        access.select_department(request, other_department.pk)

        assert access.current_department(request) == other_department

    def test_none_without_departments(self, make_user):
        assert access.current_department(request_for(make_user("nobody"))) is None
