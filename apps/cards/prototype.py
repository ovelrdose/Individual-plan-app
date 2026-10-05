"""Захардкоженные обезличенные данные для прототипа карт (фаза 0).

Время групп — из расписаний lfk.xlsx и basseyn.xlsx. ФИО и врачи вымышленные.
Модуль остаётся для тестов и команды card_prototype.
"""

from datetime import date, time, timedelta

from .xlsx.data import CardData, CardProcedure, ScheduleItem

DEPARTMENT = "ОМР № 4"
COURSE_DAYS = {3: 11, 4: 15, 5: 20}


def course_dates(start: date, days: int) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range(days))


def sample_card(shrm: int, admission_date: date = date(2026, 10, 5)) -> CardData:
    dates = course_dates(admission_date, COURSE_DAYS[shrm])
    day = dict(enumerate(dates, start=1))
    return _SAMPLES[shrm](dates, day)


def _shrm3(dates: tuple[date, ...], day: dict[int, date]) -> CardData:
    return CardData(
        department=DEPARTMENT,
        full_name="Тестова Анна Сергеевна",
        sex="Ж",
        age=17,
        doctor="Иванова А.А.",
        course_dates=dates,
        # Строки — дневная сетка слотов; пустые строки — окна пациента.
        schedule=(
            ScheduleItem(time(9, 10), "Группа Эрго общая", "зал ЛФК"),
            ScheduleItem(time(9, 50), ""),
            ScheduleItem(time(10, 30), "Группа I can нога", "зал ЛФК"),
            ScheduleItem(time(11, 15), "ЛФК в воде", "бассейн"),
            ScheduleItem(time(13, 0), "Инд.занятие"),
            ScheduleItem(time(13, 45), "st-150", "зона БОС терапии"),
            ScheduleItem(time(14, 20), "Нейротренинг", "эргозона"),
            ScheduleItem(time(15, 0), ""),
            ScheduleItem(time(15, 40), ""),
            ScheduleItem(time(16, 20), ""),
        ),
        procedures=(
            CardProcedure("Группа Эрго общая"),
            CardProcedure("Группа I can нога"),
            CardProcedure("ЛФК в воде"),
            CardProcedure("Инд.занятие"),
            CardProcedure("st-150"),
            CardProcedure("Нейротренинг"),
            CardProcedure("T.tigo"),
            # Начата с 3-го дня и отменена с 9-го — проверка серой заливки.
            CardProcedure("Алмаг", start_date=day[3], cancel_date=day[9]),
            CardProcedure("Массаж"),
            CardProcedure("Психолог"),
        ),
    )


def _shrm4(dates: tuple[date, ...], day: dict[int, date]) -> CardData:
    # Все 10 строк типичного дня и все 16 строк процедур — проверка заполненного шаблона.
    return CardData(
        department=DEPARTMENT,
        full_name="Примеров Борис Викторович",
        sex="М",
        age=52,
        doctor="Петров Б.Б.",
        course_dates=dates,
        schedule=(
            ScheduleItem(time(9, 0), "ЛФК в воде", "бассейн"),
            ScheduleItem(time(9, 50), "Группа Вест.гимнастика", "зал ЛФК"),
            ScheduleItem(time(10, 30), "Нейротренинг", "эргозона"),
            ScheduleItem(time(11, 10), "Инд.занятие"),
            ScheduleItem(time(13, 0), "Pablo", "зона БОС терапии"),
            ScheduleItem(time(13, 40), "Эрготренинг", "эргозона"),
            ScheduleItem(time(14, 20), "Инд.занятие"),
            ScheduleItem(time(15, 0), "Группа I can рука", "эргозона"),
            ScheduleItem(time(15, 40), "Группа Эрго кисть", "эргозона"),
            ScheduleItem(time(16, 20), "Логоритмика", "эргозона"),
        ),
        procedures=(
            CardProcedure("ЛФК в воде"),
            CardProcedure("Группа Вест.гимнастика"),
            CardProcedure("Нейротренинг"),
            CardProcedure("Инд.занятие"),
            CardProcedure("Pablo", start_date=day[2]),
            CardProcedure("Эрготренинг"),
            CardProcedure("Группа I can рука"),
            CardProcedure("Группа Эрго кисть"),
            CardProcedure("Логоритмика", cancel_date=day[11]),
            CardProcedure("механотерапия"),
            CardProcedure("T.tigo"),
            CardProcedure("Степпер"),
            CardProcedure("Орторент-дорожка"),
            CardProcedure("Эрготерапевт"),
            CardProcedure("Логопед"),
            CardProcedure("Психолог"),
        ),
    )


def _shrm5(dates: tuple[date, ...], day: dict[int, date]) -> CardData:
    return CardData(
        department=DEPARTMENT,
        full_name="Образцова Вера Николаевна",
        sex="Ж",
        age=41,
        doctor="Сидорова В.В.",
        course_dates=dates,
        schedule=(
            ScheduleItem(time(9, 10), "Инд.занятие"),
            ScheduleItem(time(9, 50), ""),
            ScheduleItem(time(10, 30), "Группа I can нога", "зал ЛФК"),
            ScheduleItem(time(11, 10), ""),
            ScheduleItem(time(13, 15), "Pablo/Имитрон", "зона БОС терапии"),
            ScheduleItem(time(13, 40), ""),
            ScheduleItem(time(14, 20), "Нейротренинг", "эргозона"),
            ScheduleItem(time(15, 0), "Группа I can рука", "эргозона"),
            ScheduleItem(time(15, 40), "Инд.занятие"),
            ScheduleItem(time(16, 20), ""),
        ),
        procedures=(
            CardProcedure("Инд.занятие"),
            CardProcedure("Нейротренинг"),
            CardProcedure("Группа I can нога"),
            CardProcedure("Группа I can рука"),
            # Отменён с 14-го дня — заливка попадает и во вторую таблицу дат.
            CardProcedure("Имитрон", cancel_date=day[14]),
            CardProcedure("T.tigo"),
            CardProcedure("Алмаг", start_date=day[5]),
            CardProcedure("Массаж"),
            CardProcedure("механотерапия"),
            CardProcedure("Логопед"),
            CardProcedure("Психолог"),
            CardProcedure("Эрготерапевт"),
        ),
    )


_SAMPLES = {3: _shrm3, 4: _shrm4, 5: _shrm5}
