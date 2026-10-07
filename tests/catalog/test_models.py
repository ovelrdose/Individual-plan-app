from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure, ProcedureKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def equipment():
    return Equipment.objects.create(name="st-150")


def procedure(**fields):
    defaults = {
        "name": "Эрго общая",
        "card_label": "Группа Эрго общая",
        "kind": ProcedureKind.LFK_GROUP,
    }
    return Procedure(**(defaults | fields))


class TestEquipment:
    def test_defaults(self, equipment):
        equipment.full_clean()

        assert equipment.capacity == 1
        assert equipment.start_times()[-1] == time(14, 45)

    def test_window_end_before_start(self):
        with pytest.raises(ValidationError, match="Окно"):
            Equipment(name="x", window_start=time(15, 0), window_end=time(13, 0)).full_clean()

    def test_duration_longer_than_window(self):
        with pytest.raises(ValidationError, match="не помещается"):
            Equipment(name="x", duration_min=180).full_clean()


class TestProcedure:
    def test_equipment_required_for_equipment_kind(self):
        with pytest.raises(ValidationError) as error:
            procedure(name="st-150", card_label="st-150", kind=ProcedureKind.EQUIPMENT).full_clean()
        assert "equipment" in error.value.message_dict

    def test_equipment_forbidden_for_other_kinds(self, equipment):
        with pytest.raises(ValidationError) as error:
            procedure(equipment=equipment).full_clean()
        assert "equipment" in error.value.message_dict

    def test_synonyms_are_trimmed(self):
        item = procedure(synonyms=[" Эрго общая ", "", "  "])
        item.full_clean()

        assert item.synonyms == ["Эрго общая"]

    def test_same_name_twice_for_center_is_rejected(self):
        procedure().save()

        with pytest.raises(IntegrityError):
            procedure().save()

    def test_same_name_in_department_is_allowed(self, department):
        procedure().save()
        procedure(department=department).save()

        assert Procedure.objects.count() == 2

    def test_history(self):
        item = procedure()
        item.save()
        item.place = "зал ЛФК"
        item.save()

        assert item.history.count() == 2


class TestGroupSession:
    def test_only_for_groups(self):
        individual = procedure(name="Инд", card_label="Инд.занятие", kind=ProcedureKind.INDIVIDUAL)
        individual.save()

        with pytest.raises(ValidationError) as error:
            GroupSession(procedure=individual, start_time=time(9, 10)).full_clean()
        assert "procedure" in error.value.message_dict

    def test_end_and_place(self):
        group = procedure(place="зал ЛФК")
        group.save()
        session = GroupSession.objects.create(procedure=group, start_time=time(9, 10))

        assert session.end_time == time(9, 40)
        assert session.effective_place == "зал ЛФК"
        session.place = "эргозона"
        assert session.effective_place == "эргозона"

    def test_unique_start_per_group(self):
        group = procedure()
        group.save()
        GroupSession.objects.create(procedure=group, start_time=time(9, 10))

        with pytest.raises(IntegrityError):
            GroupSession.objects.create(procedure=group, start_time=time(9, 10))


class TestInstructorSlot:
    def test_end_after_start(self):
        with pytest.raises(ValidationError):
            InstructorSlot(start=time(9, 40), end=time(9, 10)).full_clean()

    def test_overlap_is_rejected(self):
        InstructorSlot.objects.create(start=time(9, 10), end=time(9, 40))

        with pytest.raises(ValidationError, match="пересекается"):
            InstructorSlot(start=time(9, 30), end=time(10, 0)).full_clean()

    def test_touching_slots_are_fine(self):
        InstructorSlot.objects.create(start=time(9, 10), end=time(9, 40))

        InstructorSlot(start=time(9, 40), end=time(10, 10)).full_clean()

    def test_editing_itself_is_not_overlap(self):
        slot = InstructorSlot.objects.create(start=time(9, 10), end=time(9, 40))
        slot.end = time(9, 45)

        slot.full_clean()


class TestEveningIndividual:
    """Мото-Л / Артромот — признак у строки карты, а не отдельное занятие (FR-SCH-8)."""

    def test_only_for_card_only(self):
        with pytest.raises(ValidationError) as error:
            procedure(evening_individual=True).full_clean()
        assert "evening_individual" in error.value.message_dict

    def test_card_only_is_fine(self):
        procedure(
            name="Мото-Л",
            card_label="Мото-Л",
            kind=ProcedureKind.CARD_ONLY,
            evening_individual=True,
        ).full_clean()
