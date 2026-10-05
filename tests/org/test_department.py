import pytest
from django.core.exceptions import ValidationError

from apps.org.models import Department


@pytest.mark.django_db
def test_defaults():
    department = Department.objects.create(name="ОМР № 2", code="omr2")

    assert [department.course_length(shrm) for shrm in (3, 4, 5)] == [11, 15, 20]
    assert department.max_individual_per_day == 2
    department.full_clean()


@pytest.mark.parametrize(
    "course_days",
    [
        {"3": 11, "4": 15},
        {"3": 11, "4": 15, "5": 20, "6": 25},
        {"3": 11, "4": 0, "5": 20},
        {"3": 11, "4": "15", "5": 20},
        [11, 15, 20],
    ],
)
@pytest.mark.django_db
def test_invalid_course_days(course_days):
    department = Department(name="ОМР № 2", code="omr2", course_days=course_days)

    with pytest.raises(ValidationError) as error:
        department.full_clean()
    assert "course_days" in error.value.message_dict


@pytest.mark.django_db
def test_changes_are_logged():
    department = Department.objects.create(name="ОМР № 2", code="omr2")
    department.max_individual_per_day = 3
    department.save()

    assert department.history.count() == 2
    assert department.history.first().max_individual_per_day == 3
