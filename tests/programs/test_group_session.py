"""Время группы в назначении (TZ.md, решение 81): проверка модели, «Взять прошлый план»
и перенос данных из прежнего поля preferred_time."""

from datetime import date, time

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import User
from apps.catalog.models import GroupSession, Procedure, ProcedureKind
from apps.org.models import Department
from apps.programs import services
from apps.programs.models import Prescription

NAME = "Тестова Анна Сергеевна"


@pytest.fixture
def knee(db) -> Procedure:
    group = Procedure.objects.create(
        name="ДС: колено", card_label="ДС: колено", kind=ProcedureKind.DS_GROUP
    )
    for start in (time(10, 30), time(15, 0)):
        GroupSession.objects.create(procedure=group, start_time=start)
    return group


def at(group: Procedure, start: time) -> GroupSession:
    return GroupSession.objects.get(procedure=group, start_time=start)


class TestClean:
    def test_session_of_this_group(self, make_program, knee, procedures):
        program = make_program()
        item = Prescription(program=program, procedure=knee, group_session=at(knee, time(15, 0)))
        item.full_clean()

        item.group_session = GroupSession.objects.create(
            procedure=procedures["group"], start_time=time(9, 0)
        )
        with pytest.raises(ValidationError, match="действующее занятие этой группы"):
            item.full_clean()

    def test_inactive_session_and_not_a_group(self, make_program, knee, procedures):
        program = make_program()
        late = at(knee, time(15, 0))
        late.is_active = False
        late.save()

        for item in (
            Prescription(program=program, procedure=knee, group_session=late),
            Prescription(program=program, procedure=procedures["card_only"], group_session=late),
        ):
            with pytest.raises(ValidationError) as error:
                item.full_clean()
            assert "group_session" in error.value.message_dict

    def test_generic_pool_uses_pool_group(self, make_program):
        generic = Procedure.objects.create(
            name="Бассейн", card_label="Бассейн", kind=ProcedureKind.POOL, group_choice=True
        )
        back = Procedure.objects.create(name="Бассейн спина", card_label="Бассейн", kind="POOL")
        session = GroupSession.objects.create(procedure=back, start_time=time(14, 0))
        item = Prescription(program=make_program(), procedure=generic, group_session=session)

        assert item.choice_group is None and not item.session_fits()
        item.pool_group = back
        assert item.choice_group == back and item.session_fits()
        item.full_clean()


class TestCopyPlan:
    def test_active_choice_is_copied(self, doctor, make_program, knee):
        old = make_program(full_name=NAME, start_date=date(2026, 9, 1), history_number="1")
        late = at(knee, time(15, 0))
        services.add_prescription(
            doctor, Prescription(program=old, procedure=knee, group_session=late)
        )
        new = make_program(full_name=NAME, start_date=date(2026, 11, 2), history_number="2")

        services.link_previous(doctor, new, old, copy=True)

        item = new.prescriptions.get()
        assert item.group_session == late
        assert set(item.bookings.values_list("start", flat=True)) == {time(15, 0)}

    def test_disabled_choice_is_not_copied(self, doctor, make_program, knee):
        old = make_program(full_name=NAME, start_date=date(2026, 9, 1), history_number="1")
        late = at(knee, time(15, 0))
        Prescription.objects.create(program=old, procedure=knee, group_session=late)
        late.is_active = False
        late.save()
        new = make_program(full_name=NAME, start_date=date(2026, 11, 2), history_number="2")

        services.link_previous(doctor, new, old, copy=True)

        assert new.prescriptions.get().group_session is None


@pytest.mark.django_db(transaction=True)
def test_migration_moves_preferred_time_to_session():
    """0012: время из листа становится ссылкой на действующее занятие группы, у «Бассейн» без
    группы — группы бассейна; времени без занятия — выбора нет. Потом поле удаляется."""
    before = [("programs", "0011_prescription_group_session")]
    after = [("programs", "0013_remove_prescription_preferred_time")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    # Справочники и пользователи этой миграцией не меняются — берём текущие модели; программа
    # и назначение — из состояния до миграции (с полем preferred_time), связи — по id.
    old = executor.loader.project_state(before).apps
    Program = old.get_model("programs", "Program")
    OldPrescription = old.get_model("programs", "Prescription")

    department = Department.objects.create(name="ОМР № 4", code="omr4")
    doctor = User.objects.create(username="doctor", is_attending_doctor=True)
    program = Program.objects.create(
        department_id=department.pk,
        full_name=NAME,
        room="5",
        shrm=4,
        attending_doctor_id=doctor.pk,
        start_date=date(2026, 10, 5),
        end_date=date(2026, 10, 19),
    )
    knee = Procedure.objects.create(name="Колено", card_label="Колено", kind="DS_GROUP")
    generic = Procedure.objects.create(
        name="Бассейн", card_label="Бассейн", kind="POOL", group_choice=True
    )
    back = Procedure.objects.create(name="Бассейн спина", card_label="Бассейн", kind="POOL")
    knee_late = GroupSession.objects.create(procedure=knee, start_time=time(15, 0))
    GroupSession.objects.create(procedure=knee, start_time=time(16, 0), is_active=False)
    back_late = GroupSession.objects.create(procedure=back, start_time=time(14, 0))

    def make(procedure, preferred, pool_group=None):
        return OldPrescription.objects.create(
            program_id=program.pk,
            procedure_id=procedure.pk,
            preferred_time=preferred,
            pool_group_id=pool_group.pk if pool_group else None,
        ).pk

    found = make(knee, time(15, 0))
    inactive = make(knee, time(16, 0))
    missing = make(knee, time(9, 0))
    pool = make(generic, time(14, 0), pool_group=back)
    none = make(knee, None)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    NewPrescription = new.get_model("programs", "Prescription")
    chosen = dict(NewPrescription.objects.values_list("pk", "group_session_id"))

    assert chosen == {
        found: knee_late.pk,
        inactive: None,
        missing: None,
        pool: back_late.pk,
        none: None,
    }
    assert "preferred_time" not in {f.name for f in NewPrescription._meta.get_fields()}
    # Тест остаётся на последней схеме: вернуть все миграции до конца.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
