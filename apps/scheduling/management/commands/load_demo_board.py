"""Демо-шахматка: база пациентов и расписания — как на primary_docs/plan.jpg (шахматка 06.10).

Удаляет всех пациентов и собирает заново: вымышленные пациенты владельца, их назначения,
индивидуальные в шахматке на дату и время на тренажёрах — клетка в клетку с картинкой.
Распорядок инструкторов приводится к картинке с этой даты. Только для разработки и показа.

    .venv/bin/python manage.py load_demo_board            # на сегодняшнюю шахматку
    .venv/bin/python manage.py load_demo_board --date 2026-10-07 --yes

Одна фамилия — один пациент, пока это не нарушает правил (не больше двух индивидуальных,
не два места в одно время, без двух индивидуальных подряд); иначе — однофамилец в той же палате.
"""

from dataclasses import dataclass, field
from datetime import date, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Membership, Role, User
from apps.catalog.domain import add_minutes
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure
from apps.org.models import Department
from apps.programs import services as programs
from apps.programs.models import Prescription, Program
from apps.scheduling import board, board_days, manual, staff_changes
from apps.scheduling.models import Booking, BookingKind
from apps.staff.models import Instructor
from apps.staff.services import instructor_calendars

# Индивидуальные: (время, инструктор, отделение, палата, фамилия, пометка). Пара 2/2 —
# работающий в этот день член пары; пустое отделение — ОМР № 4.
INDIVIDUAL = [
    ("9:10", "Ким", "", "14", "Гришина", ""),
    ("9:10", "Любимский-Печерских", "", "14", "Рехтин", ""),
    ("9:10", "Атаманчук", "", "9", "Пашкова", ""),
    ("9:50", "Ким", "", "7", "Слабодчикова", ""),
    ("9:50", "Любимский-Печерских", "", "14", "Кутькин", ""),
    ("9:50", "Атаманчук", "", "10", "Маркин", ""),
    ("9:50", "Овсиенко", "", "2", "Казанцева", ""),
    ("9:50", "Логвинов", "ОМР 3", "6", "Печенин", ""),
    ("10:30", "Ким", "", "9", "Дубровская", ""),
    ("10:30", "Любимский-Печерских", "", "11", "Логиновский", ""),
    ("10:30", "Атаманчук", "", "9", "Шмидт", ""),
    ("10:30", "Логвинов", "", "5", "Фадеев", ""),
    ("11:10", "Ким", "", "11", "Потапов", ""),
    ("11:10", "Любимский-Печерских", "", "5", "Воронин", ""),
    ("11:10", "Атаманчук", "", "5а", "Плиев", ""),
    ("11:10", "Овсиенко", "", "1", "Сумской", ""),
    ("11:10", "Логвинов", "", "11", "Филиппов", ""),
    ("13:00", "Ким", "", "9", "Злобина", ""),
    ("13:00", "Любимский-Печерских", "", "11", "Маничев", ""),
    ("13:00", "Атаманчук", "", "1", "Астахов", ""),
    ("13:40", "Ким", "", "9", "Пашкова", ""),
    ("13:40", "Любимский-Печерских", "", "2", "Сбитнева", ""),
    ("13:40", "Атаманчук", "", "4", "Романова", ""),
    ("14:20", "Ким", "", "1", "Кадников", ""),
    ("14:20", "Любимский-Печерских", "", "3", "Ивонин", ""),
    ("14:20", "Атаманчук", "", "1", "Бородич", ""),
    ("15:00", "Любимский-Печерских", "ОМР 3", "8", "Меркульева", ""),
    ("15:00", "Овсиенко", "", "14", "Рехтин", ""),
    ("15:40", "Ким", "", "11", "Бородин", ""),
    ("15:40", "Любимский-Печерских", "", "9", "Дубровская", ""),
    ("15:40", "Овсиенко", "", "14", "Кутькин", ""),
    ("16:20", "Любимский-Печерских", "", "11", "Потапов", ""),
    ("16:20", "Овсиенко", "", "14", "Гришина", ""),
    ("18:00", "Ким", "", "2", "Казанцева", "art"),
    ("18:40", "Ким", "", "14", "Кутькин", "art"),
    ("19:20", "Ким", "", "9", "Дубровская", "Мото-Л art"),
]
# Тренажёры: (время, тренажёр, отделение, палата, фамилия, пометка).
EQUIPMENT = [
    ("13:00", "st-150", "", "3", "Власенко", ""),
    ("13:00", "st-150", "", "2", "Денисова", ""),
    ("13:00", "Имитрон", "", "3", "Ивонин", ""),
    ("13:00", "Имитрон", "", "11", "Бородин", ""),
    ("13:00", "Pablo", "", "5", "Воронин", ""),
    ("13:15", "st-150", "", "4", "Жуйкова", ""),
    ("13:15", "Имитрон", "", "11", "Маничев", ""),
    ("13:15", "Pablo", "", "5а", "Плиев", ""),
    ("13:30", "st-150", "", "5", "Воронин", ""),
    ("13:30", "st-150", "", "12", "Мовсесян", ""),
    ("13:30", "Имитрон", "", "1", "Астахов", ""),
    ("13:30", "Pablo", "", "3", "Власенко", ""),
    ("13:45", "st-150", "", "1", "Астахов", ""),
    ("13:45", "Имитрон", "", "5", "Воронин", ""),
    ("13:45", "Pablo", "", "8", "Табачкова", "(шар)"),
    ("14:00", "st-150", "ОМР1", "16", "Мазолова", ""),
    ("14:00", "Имитрон", "", "7", "Слабодчикова", ""),
    ("14:00", "Pablo", "", "1", "Бородич", ""),
    ("14:15", "st-150", "", "2", "Кухарева", ""),
    ("14:15", "Pablo", "", "4", "Жуйкова", ""),
    ("14:30", "st-150", "", "1", "Горшков", ""),
    ("14:30", "st-150", "", "7", "Слабодчикова", ""),
    ("14:30", "Имитрон", "", "2", "Казанцева", ""),
    ("14:30", "Pablo", "", "11", "Бородин", ""),
]
# На картинке на st и Имитроне — по двое в одно время.
CAPACITY = {"st-150": 2, "Имитрон": 2}
HOME = "ОМР № 4"


def minutes(text: str) -> int:
    hours, mins = text.split(":")
    return int(hours) * 60 + int(mins)


def clock(text: str) -> time:
    return time(*map(int, text.split(":")))


@dataclass
class Person:
    """Пациент с картинки: что у него в этот день."""

    department: str
    room: str
    surname: str
    individual: list[tuple[str, str, str]] = field(default_factory=list)  # время, кто, пометка
    equipment: list[tuple[str, str, str]] = field(default_factory=list)  # время, что, пометка
    busy: list[tuple[int, int]] = field(default_factory=list)

    def fits(self, at: str, individual: bool) -> bool:
        start = minutes(at)
        end = start + (30 if individual else 15)
        if any(s < end and start < e for s, e in self.busy):
            return False
        if individual:
            if len(self.individual) >= 2:
                return False
            # Два индивидуальных подряд (перерыв не больше 10 минут) — нельзя (FR-SCH-10).
            for other, _who, _note in self.individual:
                s, e = minutes(other), minutes(other) + 30
                if abs(start - e) <= 10 or abs(s - end) <= 10:
                    return False
        return True

    def add(self, at: str, what: str, note: str, individual: bool) -> None:
        start = minutes(at)
        self.busy.append((start, start + (30 if individual else 15)))
        (self.individual if individual else self.equipment).append((at, what, note))


def people() -> list[Person]:
    """Записи с картинки — по пациентам. Одна фамилия — один пациент, пока это не нарушает
    правил; иначе — однофамилец (картинка заполнена вручную, пациенты бывают разные)."""
    rows = [(at, what, d, r, s, n, True) for at, what, d, r, s, n in INDIVIDUAL] + [
        (at, what, d, r, s, n, False) for at, what, d, r, s, n in EQUIPMENT
    ]
    result: list[Person] = []
    for at, what, department, room, surname, note, individual in sorted(
        rows, key=lambda row: minutes(row[0])
    ):
        same = [
            p for p in result if (p.department, p.room, p.surname) == (department, room, surname)
        ]
        person = next((p for p in same if p.fits(at, individual)), None)
        if person is None:
            person = Person(department, room, surname)
            result.append(person)
        person.add(at, what, note, individual)
    return result


class Command(BaseCommand):
    help = "Демо: пациенты и расписание как на primary_docs/plan.jpg (удаляет всех пациентов)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--date",
            help="Дата шахматки, ГГГГ-ММ-ДД: сегодняшняя или завтрашняя (по умолчанию — сегодня).",
        )
        parser.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение.")

    def handle(self, *args, **options) -> None:
        if not settings.DEBUG:
            raise CommandError(
                "Только для разработки (DEBUG=true): команда удаляет всех пациентов."
            )
        dates = board_days.board_dates(board_days.today())
        day = date.fromisoformat(options["date"]) if options["date"] else dates[0]
        if day not in dates:
            raise CommandError(
                "Шахматку можно править только на сегодня и следующий будний день: "
                + ", ".join(f"{d:%d.%m.%Y}" for d in dates)
                + "."
            )
        admin = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        if admin is None:
            raise CommandError("Нужен администратор — запустите seed_dev.")
        doctor = User.objects.filter(memberships__role=Role.DOCTOR, is_active=True).first()
        if doctor is None:
            raise CommandError("Нужен врач — запустите seed_dev.")
        missing = {who for _at, who, *_rest in INDIVIDUAL} - set(
            Instructor.objects.filter(is_active=True).values_list("short_name", flat=True)
        )
        if missing:
            raise CommandError(
                "Нет инструкторов с картинки: "
                + ", ".join(sorted(missing))
                + ". Сначала load_board_instructors."
            )
        count = Program.objects.count()
        if not options["yes"] and count:
            answer = input(
                f"Удалить всех пациентов ({count}) и собрать шахматку {day:%d.%m}? [yes/no] "
            )
            if answer.strip().lower() not in ("yes", "y", "да"):
                raise CommandError("Отменено.")

        with transaction.atomic():
            built = self._build(admin, doctor, day)
        self.stdout.write(
            self.style.SUCCESS(f"Готово: {built} пациентов, шахматка на {day:%d.%m.%Y}.")
        )

    def _build(self, admin: User, doctor: User, day: date) -> int:
        departments = {"": Department.objects.get(name=HOME)}
        for name in {d for _a, _w, d, *_r in INDIVIDUAL + EQUIPMENT if d}:
            departments[name], _ = Department.objects.get_or_create(name=name)
        for department in departments.values():
            Membership.objects.get_or_create(
                user=doctor, department=department, defaults={"role": Role.DOCTOR}
            )
        instructors = {i.short_name: i for i in Instructor.objects.filter(is_active=True)}
        slots = {f"{s.start:%-H:%M}": s for s in InstructorSlot.objects.all()}
        procedures = {p.name: p for p in Procedure.objects.all()}
        equipment = {e.name: e for e in Equipment.objects.all()}

        for program in Program.objects.all():
            programs.delete_program(admin, program)
        board_days.ensure_boards()
        self._staff(admin, instructors, slots, day)
        for name, capacity in CAPACITY.items():
            Equipment.objects.filter(name=name).update(capacity=capacity)

        persons = people()
        made: list[tuple[Person, Program]] = []
        for number, person in enumerate(persons, start=1):
            female = person.surname.endswith(("а", "я"))
            program = Program(
                department=departments[person.department],
                full_name=f"{person.surname} {'Анна Сергеевна' if female else 'Иван Сергеевич'}",
                sex="Ж" if female else "М",
                history_number=f"D{number:03d}",
                room=person.room,
                shrm=4,
                attending_doctor=doctor,
                # Поступил накануне: дата шахматки — первый день занятий.
                start_date=day - timedelta(days=1),
            )
            programs.save_program(admin, program, end_date_changed=False)
            if person.individual:
                self._prescribe(
                    admin, program, procedures["Индивидуальное занятие"], len(person.individual)
                )
            if any("Мото-Л" in note for *_x, note in person.individual):
                self._prescribe(admin, program, procedures["Мото-Л"], 1)
            for name in sorted({what for _at, what, _n in person.equipment}):
                per_day = sum(1 for _at, what, _n in person.equipment if what == name)
                self._prescribe(admin, program, procedures[name], per_day)
            made.append((person, program))

        # Сначала тренажёры — на время с картинки, потом индивидуальные: иначе подбор мог
        # поставить тренажёр на время будущего индивидуального.
        self._equipment(admin, made, equipment, day)
        for person, program in made:
            for at, who, note in sorted(person.individual, key=lambda x: minutes(x[0])):
                person_at = self._working(instructors[who], day)
                booking = board.place_patient(admin, program, person_at, slots[at], day)
                if note:
                    booking.refresh_from_db()
                    board.set_note(admin, booking, booking.version, note)
        return len(made)

    def _working(self, instructor: Instructor, day: date) -> Instructor:
        """На картинке колонка пары 2/2 подписана одним из пары — ставим к работающему."""
        partner = instructor.partner if instructor.partner_id else None
        people_ = [instructor] + ([partner] if partner else [])
        calendars = instructor_calendars(people_, day, day)
        return next((p for p in people_ if calendars[p.pk].is_working(day)), instructor)

    def _prescribe(self, admin: User, program: Program, procedure: Procedure, per_day: int) -> None:
        programs.add_prescription(
            admin, Prescription(program=program, procedure=procedure, per_day=per_day)
        )

    def _staff(self, admin: User, instructors: dict, slots: dict, day: date) -> None:
        """Распорядок с картинки с этой даты: «I can нога» в 10:30 ведёт Овсиенко, у Логвинова
        в 9:10 — «Метод. работа»; Чернышова на картинке нет — в этот день не работает."""
        noga = GroupSession.objects.get(procedure__name="I can нога", start_time=time(10, 30))
        for name in ("Шиндер", "Любимский-Печерских"):
            duty = board.duty_at(instructors[name], slots["10:30"], day)
            if duty is not None and duty.group_session_id == noga.pk:
                board.end_duty(admin, instructors[name], slots["10:30"], day, with_partner=False)
        current = board.duty_at(instructors["Овсиенко"], slots["10:30"], day)
        if current is None or current.group_session_id != noga.pk:
            board.add_duty(
                admin,
                instructors["Овсиенко"],
                slots["10:30"],
                day,
                "GROUP_LEAD",
                group_session=noga,
            )
        if board.duty_at(instructors["Логвинов"], slots["9:10"], day) is None:
            board.add_duty(admin, instructors["Логвинов"], slots["9:10"], day, "METHOD_WORK")
        if "Чернышов" in instructors:
            staff_changes.set_not_working(admin, instructors["Чернышов"], day)

    def _equipment(self, admin: User, made: list, equipment: dict, day: date) -> None:
        """Время на тренажёрах в эту дату — как на картинке, закреплено (в другие дни — подбор).
        Записи дня пересоздаются целиком: переставлять по одной нельзя — место может занимать
        другой тренажёр, который сам ждёт переноса. Пациент не в двух местах — проверяет база."""
        for person, program in made:
            program.bookings.filter(date=day, kind=BookingKind.EQUIPMENT).delete()
            by_procedure = {
                p.procedure.name: p for p in program.prescriptions.select_related("procedure")
            }
            for at, what, note in person.equipment:
                item = equipment[what]
                prescription = by_procedure[what]
                booking = Booking(
                    program=program,
                    prescription=prescription,
                    procedure=prescription.procedure,
                    kind=BookingKind.EQUIPMENT,
                    date=day,
                    start=clock(at),
                    end=add_minutes(clock(at), item.duration_min),
                    equipment=item,
                    note=note,
                )
                manual.save_manual(admin, booking)
