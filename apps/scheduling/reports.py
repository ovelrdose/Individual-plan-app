"""Отчёт «Загрузка инструкторов» за период (TZ.md FR-SCH-17).

Считаются индивидуальные занятия с инструктором и рабочие будние дни: в выходные инструкторам
индивидуальные не расписываются (решение 56), поэтому и среднее — на рабочий будний день.
Для пары 2/2 — строка суммы.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction

from apps.accounts.models import User
from apps.staff.models import Instructor
from apps.staff.services import build_teams, instructor_calendars, require_staff_manager

from .domain import is_weekend
from .models import Booking, BookingKind

MAX_DAYS = 366


@dataclass(frozen=True)
class LoadRow:
    name: str
    sessions: int
    days: int
    is_total: bool = False

    @property
    def average(self) -> Fraction | None:
        return Fraction(self.sessions, self.days) if self.days else None


@dataclass
class LoadReport:
    start: date
    end: date
    rows: list[LoadRow] = field(default_factory=list)


def instructor_load(user: User, start: date, end: date) -> LoadReport:
    require_staff_manager(user)
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=MAX_DAYS - 1))
    teams = build_teams()
    ids = [pk for team in teams for pk in team.member_ids]
    instructors = {item.pk: item for item in Instructor.objects.filter(pk__in=ids)}
    calendars = instructor_calendars(list(instructors.values()), start, end)
    weekdays = [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if not is_weekend(start + timedelta(days=i))
    ]
    sessions = Counter(
        Booking.objects.filter(
            kind=BookingKind.INDIVIDUAL, instructor_id__in=ids, date__range=(start, end)
        )
        .exclude(date__iso_week_day__gte=6)
        .values_list("instructor_id", flat=True)
    )
    report = LoadReport(start, end)
    for team in teams:
        members = []
        for member in team.members:
            days = sum(calendars[member.id].is_working(day) for day in weekdays)
            members.append(LoadRow(member.short_name, sessions[member.id], days))
        report.rows += members
        if len(members) > 1:
            report.rows.append(
                LoadRow(
                    team.label,
                    sum(row.sessions for row in members),
                    sum(row.days for row in members),
                    is_total=True,
                )
            )
    return report
