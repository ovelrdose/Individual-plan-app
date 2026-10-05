"""Выгрузка карты программы (TZ.md §8)."""

from datetime import time

from apps.accounts.models import User
from apps.programs.models import Program
from apps.scheduling.services import program_schedule

from .card_templates import bundled_mappings, bundled_template
from .models import CardExport
from .xlsx.data import CardData, CardProcedure, ScheduleItem
from .xlsx.writer import CardRenderError, render_card


def build_card_data(program: Program) -> CardData:
    prescriptions = list(program.prescriptions.filter(in_card=True).select_related("procedure"))
    unrecognized = [p.raw_text for p in prescriptions if p.procedure is None]
    if unrecognized:
        raise CardRenderError(
            "Сначала выберите процедуру для нераспознанных назначений: "
            + "; ".join(unrecognized)
            + "."
        )
    return CardData(
        department=program.department.name,
        full_name=program.full_name,
        sex=program.sex,
        age=program.age,
        doctor=str(program.attending_doctor),
        course_dates=tuple(program.course_dates()),
        # Верхний блок «Расписание занятий» — типичный день по сетке слотов, с окнами (FR-CRD-2).
        schedule=tuple(
            ScheduleItem(time(row.start // 60, row.start % 60), row.label, row.place)
            for row in program_schedule(program).day
        ),
        procedures=tuple(
            CardProcedure(p.procedure.card_label, p.start_date, p.cancel_date)
            for p in prescriptions
        ),
    )


def card_filename(program: Program) -> str:
    parts = ["Программа", program.surname]
    if program.history_number:
        parts.append(program.history_number)
    parts.append(f"{program.start_date:%d.%m.%Y}")
    return "_".join(parts) + ".xlsx"


def choose_template(shrm: int, days: int) -> int:
    """Шаблон карты: по ШРМ программы, а если продлённый курс не помещается — самый
    маленький шаблон, где хватает дат (решение владельца 04.10: ШРМ 4 на 18 дней печатается
    на шаблоне ШРМ 5). Шаблон пока общий для всех отделений."""
    mappings = bundled_mappings()
    if mappings[shrm].max_dates >= days:
        return shrm
    fitting = sorted(
        (mapping.max_dates, key) for key, mapping in mappings.items() if mapping.max_dates >= days
    )
    if not fitting:
        largest = max(mapping.max_dates for mapping in mappings.values())
        raise CardRenderError(
            f"Дней курса {days}, а самый большой шаблон карты вмещает {largest}. "
            "Сократите курс или разбейте его на две программы."
        )
    return fitting[0][1]


def export_card(user: User, program: Program) -> tuple[str, bytes]:
    data = build_card_data(program)
    template = choose_template(program.shrm, len(data.course_dates))
    content = render_card(bundled_template(template), bundled_mappings()[template], data)
    CardExport.objects.create(program=program, user=user)
    return card_filename(program), content
