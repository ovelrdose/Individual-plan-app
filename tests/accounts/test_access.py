import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory

from apps.accounts import access
from apps.accounts.models import Role


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
        user = make_user("both", (department, Role.DOCTOR), (other_department, Role.REHAB))

        assert set(access.departments_for(user)) == {department, other_department}
        assert access.role_in(user, department) == Role.DOCTOR
        assert access.role_in(user, other_department) == Role.REHAB


class TestRoles:
    @pytest.mark.parametrize(
        ("user_fixture", "allowed", "expected"),
        [
            ("doctor", (Role.DOCTOR,), True),
            ("doctor", (Role.REHAB,), False),
            ("doctor", (Role.DOCTOR, Role.REHAB), True),
            ("rehab", (Role.REHAB,), True),
            ("rehab", (Role.DOCTOR,), False),
            ("admin_user", (Role.DOCTOR,), True),
            ("admin_user", (), True),
        ],
    )
    def test_has_role(self, request, department, user_fixture, allowed, expected):
        user = request.getfixturevalue(user_fixture)

        assert access.has_role(user, department, *allowed) is expected

    def test_no_role_in_foreign_department(self, doctor, other_department):
        assert access.role_in(doctor, other_department) is None
        assert not access.has_role(doctor, other_department, Role.DOCTOR, Role.REHAB)

    def test_inactive_department_denies_members(self, doctor, department):
        department.is_active = False
        department.save()

        assert not access.has_role(doctor, department, Role.DOCTOR)

    def test_require_role(self, doctor, department):
        access.require_role(doctor, department, Role.DOCTOR)
        with pytest.raises(PermissionDenied):
            access.require_role(doctor, department, Role.REHAB)


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
        user = make_user("both", (department, Role.DOCTOR), (other_department, Role.DOCTOR))
        request = request_for(user)

        access.select_department(request, other_department.pk)

        assert access.current_department(request) == other_department

    def test_none_without_departments(self, make_user):
        assert access.current_department(request_for(make_user("nobody"))) is None
