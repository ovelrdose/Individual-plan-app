from datetime import time

import pytest

from apps.catalog.models import GroupSession, InstructorSlot
from apps.staff.models import Instructor
from apps.staff.services import set_partner


@pytest.fixture
def slots(db) -> dict[str, InstructorSlot]:
    """Часть сетки слотов: ключ — начало «9:10»."""
    grid = [
        (time(9, 10), time(9, 40)),
        (time(10, 30), time(11, 0)),
        (time(11, 10), time(11, 40)),
        (time(13, 0), time(13, 30)),
        (time(15, 0), time(15, 30)),
    ]
    return {
        f"{start:%-H:%M}": InstructorSlot.objects.create(start=start, end=end)
        for start, end in grid
    }


@pytest.fixture
def ergo_session(procedures) -> GroupSession:
    return GroupSession.objects.create(procedure=procedures["group"], start_time=time(9, 10))


@pytest.fixture
def instructors(db) -> dict[str, Instructor]:
    """Вымышленные инструкторы: Волков и Лебедева — пара 2/2, Соколов — без пары."""
    team = {
        name: Instructor.objects.create(short_name=name, full_name=name, display_order=order)
        for order, name in [(10, "Волков"), (30, "Соколов"), (40, "Лебедева")]
    }
    set_partner(team["Волков"], team["Лебедева"])
    return team
