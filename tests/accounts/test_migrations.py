"""Перенос ролей в права (accounts.0003, решение 79) — на старой схеме с настоящими ролями."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("accounts", "0002_initial")]
AFTER = [("accounts", "0003_user_rights")]


@pytest.mark.django_db(transaction=True)
def test_roles_become_rights():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    Department = old.get_model("org", "Department")
    User = old.get_model("accounts", "User")
    Membership = old.get_model("accounts", "Membership")
    Group = old.get_model("auth", "Group")
    department = Department.objects.create(name="ОМР № 4", code="omr4")
    other = Department.objects.create(name="ОМР № 1", code="omr1")
    doctor = User.objects.create(username="doctor")
    rehab = User.objects.create(username="rehab", is_staff=True)
    both = User.objects.create(username="both")
    User.objects.create(username="admin", is_superuser=True, is_staff=True)
    Membership.objects.create(user=doctor, department=department, role="doctor")
    Membership.objects.create(user=rehab, department=department, role="rehab")
    Membership.objects.create(user=both, department=department, role="doctor")
    Membership.objects.create(user=both, department=other, role="rehab")
    rehab.groups.add(Group.objects.create(name="Специалист ФР"))

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    users = {u.username: u for u in new.get_model("accounts", "User").objects.all()}
    rehab_rights = ["programs_edit", "programs_delete", "schedule", "group_schedule", "staff",
                    "catalog"]  # fmt: skip

    assert (users["doctor"].rights, users["doctor"].is_attending_doctor) == (
        ["programs_edit"],
        True,
    )
    assert (users["rehab"].rights, users["rehab"].is_attending_doctor) == (rehab_rights, False)
    assert users["rehab"].is_staff and not users["doctor"].is_staff
    assert (users["both"].rights, users["both"].is_attending_doctor) == (rehab_rights, True)
    assert users["admin"].is_superuser and users["admin"].is_staff and users["admin"].rights == []
    memberships = new.get_model("accounts", "Membership").objects
    assert memberships.filter(user__username="both").count() == 2, "отделения сохраняются"
    assert not new.get_model("auth", "Group").objects.filter(name="Специалист ФР").exists()

    # Тестовая база остаётся на последней схеме для следующих тестов.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
