"""Текущие пациенты ОМР № 4 и расписание — по сводкам постов и расписанию на 15.10.2026.

Удаляет всех пациентов и собирает заново:

* пациенты, врачи, даты поступления и выписки, ИБ, возраст и пол — из сводок постов
  (``primary_docs/Svodka_*.docx``, читаются при запуске; диагноз не читается, решение 70);
* индивидуальные занятия и тренажёры на дату — клетка в клетку с ``primary_docs/planomr4.jpg``
  (раскладка перенесена ниже: палата, фамилия, время — без других данных);
* распорядок инструкторов в ячейках, которые видны на картинке, и пара «Паршуков/ Овсиенко»
  (Ким больше не работает) — с этой даты.

    .venv/bin/python manage.py load_current_patients               # расписание на сегодня
    .venv/bin/python manage.py load_current_patients --date 2026-10-16 --yes

Принятые правила (решение 72): все пациенты — в ОМР № 4, в том числе из других отделений;
палата пациента из расписания — как на картинке (её видят инструкторы, ПИТ — 14п), остальных —
из сводки; Кухарева и Казанцева с картинки в сводках не значатся и не заводятся. Врачи сводки
заводятся пользователями без пароля — войти смогут, когда администратор задаст пароль.
ШРМ в сводке нет: он выбирается по длине курса, дата выписки ставится как в сводке; если её
нет — по ШРМ 4.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.accounts.models import Membership, User
from apps.catalog.domain import add_minutes
from apps.catalog.models import Equipment, GroupSession, InstructorSlot, Procedure
from apps.exchange.patient_summary import SummaryError, SummaryPatient, read_summary
from apps.org.models import Department
from apps.programs import services as programs
from apps.programs.domain import course_end
from apps.programs.models import Prescription, Program
from apps.scheduling import board, board_days, manual, staff_changes
from apps.scheduling.models import Booking, BookingKind
from apps.staff import services as staff
from apps.staff.models import DutyKind, Instructor, ShiftPatternKind
from apps.staff.services import instructor_calendars

DEPARTMENT_CODE = "omr4"
DEFAULT_SHRM = 4

# Индивидуальные: (время, колонка — один из пары, палата, фамилия, пометка). У пары 2/2
# пациент встаёт к тому, кто работает в этот день. «36п Тарасенко» на картинке — палата 3
# (так в сводке и у соседей по палате).
INDIVIDUAL = [
    ("9:10", "Паршуков", "14", "Гришина", ""),
    ("9:50", "Паршуков", "2", "Юдина", ""),
    ("10:30", "Паршуков", "14", "Брыкин", ""),
    ("11:10", "Паршуков", "11", "Потапов", ""),
    ("15:40", "Паршуков", "11", "Бородин", ""),
    ("16:20", "Паршуков", "6", "Деризин", ""),
    ("18:00", "Паршуков", "5", "Гамбаров", ""),
    ("18:40", "Паршуков", "14", "Дружинин", ""),
    ("19:20", "Паршуков", "5а", "Шелепов", ""),
    ("9:50", "Шиндер", "11", "Дорофеев", ""),
    ("10:30", "Шиндер", "5", "Гамбаров", ""),
    ("11:10", "Шиндер", "1", "Крылов", ""),
    ("13:00", "Шиндер", "3", "Лисицин", ""),
    ("13:40", "Шиндер", "2", "Сбитнева", ""),
    ("14:20", "Шиндер", "5", "Рощин", ""),
    ("15:00", "Шиндер", "4", "Чертакова", ""),
    ("15:40", "Шиндер", "14", "Брыкин", ""),
    ("16:20", "Шиндер", "11", "Потапов", ""),
    ("9:10", "Атаманчук", "9", "Жбанникова", ""),
    ("10:30", "Атаманчук", "9", "Шмидт", ""),
    ("11:10", "Атаманчук", "7", "Данченко", ""),
    ("13:00", "Атаманчук", "9", "Злобина", ""),
    ("13:40", "Атаманчук", "8", "Малюк", ""),
    ("14:20", "Атаманчук", "1", "Бородич", ""),
    ("9:10", "Чернышов", "5а", "Шелепов", ""),
    ("9:50", "Чернышов", "9", "Горемыкина", ""),
    ("11:10", "Чернышов", "1", "Сумской", ""),
    ("13:00", "Чернышов", "1", "Астахов", ""),
    ("13:40", "Чернышов", "3", "Тарасенко", ""),
    ("14:20", "Чернышов", "8", "Плаунов", ""),
    ("9:50", "Логвинов", "6", "Печенин", ""),
    ("11:10", "Логвинов", "11", "Филиппов", ""),
]
# Тренажёры: (время, тренажёр, палата — пусто, если на картинке нет, фамилия, пометка).
EQUIPMENT = [
    ("13:00", "st-150", "2", "Денисова", ""),
    ("13:00", "Имитрон", "11", "Бородин", ""),
    ("13:15", "st-150", "4", "Жуйкова", ""),
    ("13:15", "st-150", "13", "Боброва", ""),
    ("13:15", "Имитрон", "4", "Чертакова", ""),
    ("13:30", "st-150", "3", "Фатьянов", ""),
    ("13:30", "Имитрон", "1", "Астахов", ""),
    ("13:30", "Pablo", "", "Сошникова", "(шар)"),
    ("13:45", "st-150", "1", "Астахов", ""),
    ("14:00", "st-150", "13", "Немолча", ""),
    ("14:00", "Имитрон", "1", "Баженов", ""),
    ("14:00", "Pablo", "1", "Бородич", "(шар)"),
    ("14:15", "Имитрон", "7", "Данченко", ""),
    ("14:15", "Pablo", "4", "Жуйкова", ""),
    ("14:30", "Pablo", "11", "Бородин", ""),
]
# Распорядок, видный на картинке: (колонка, время, вид, группа). Для пары — у обоих.
# Пустые ячейки не трогаем; ячейки с пациентом освобождаются от распорядка.
DUTIES = [
    ("Паршуков", "13:00", DutyKind.BOS, None),
    ("Паршуков", "13:40", DutyKind.BOS, None),
    ("Паршуков", "14:20", DutyKind.BOS, None),
    ("Паршуков", "15:00", DutyKind.GROUP_LEAD, "I can рука"),
    ("Шиндер", "9:10", DutyKind.GROUP_LEAD, "Эрго общая"),
    ("Шиндер", "18:00", DutyKind.METHOD_WORK, None),
    ("Шиндер", "18:40", DutyKind.METHOD_WORK, None),
    ("Шиндер", "19:20", DutyKind.METHOD_WORK, None),
    ("Атаманчук", "15:00", DutyKind.METHOD_WORK, None),
    ("Атаманчук", "15:40", DutyKind.METHOD_WORK, None),
    ("Атаманчук", "16:20", DutyKind.METHOD_WORK, None),
    ("Чернышов", "15:00", DutyKind.METHOD_WORK, None),
    ("Чернышов", "15:40", DutyKind.METHOD_WORK, None),
    ("Чернышов", "16:20", DutyKind.METHOD_WORK, None),
    ("Логвинов", "9:10", DutyKind.METHOD_WORK, None),
    ("Логвинов", "10:30", DutyKind.GROUP_LEAD, "I can нога"),
]
# Пара на картинке: «Паршуков/ Овсиенко»; Ким больше не работает.
PAIR = ("Паршуков", "Овсиенко")
LEFT = "Ким"

TRANSLIT = str.maketrans("абвгдезийклмнопрстуфыэ", "abvgdeziyklmnoprstufye") | {
    ord(letter): latin
    for letter, latin in {
        "ё": "e",
        "ж": "zh",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ь": "",
        "ю": "yu",
        "я": "ya",
    }.items()
}


def minutes(text: str) -> int:
    hours, mins = text.split(":")
    return int(hours) * 60 + int(mins)


def clock(text: str) -> time:
    return time(*map(int, text.split(":")))


def shrm_for(days: int) -> int:
    """ШРМ по длине курса в календарных днях: 11 — ШРМ 3, 15 — ШРМ 4, больше — ШРМ 5."""
    if days <= 11:
        return 3
    return 4 if days <= 15 else 5


def login_for(short_name: str) -> str:
    return short_name.split()[0].lower().translate(TRANSLIT)


@dataclass
class Person:
    """Пациент сводки и что у него в расписании на дату."""

    patient: SummaryPatient
    room: str
    individual: list[tuple[str, str, str]]  # время, колонка, пометка
    equipment: list[tuple[str, str, str]]  # время, тренажёр, пометка


def match(patients: list[SummaryPatient]) -> list[Person]:
    """Раскладка картинки — к пациентам сводки по фамилии. Чего нет в сводке — ошибка."""
    by_surname: dict[str, list[SummaryPatient]] = {}
    for patient in patients:
        by_surname.setdefault(patient.surname, []).append(patient)
    duplicated = sorted(name for name, found in by_surname.items() if len(found) > 1)
    if duplicated:
        raise CommandError("В сводках однофамильцы — уточните раскладку: " + ", ".join(duplicated))
    persons = {p.surname: Person(p, p.room, [], []) for p in patients}
    missing = []
    for at, column, room, surname, note in INDIVIDUAL:
        if surname not in persons:
            missing.append(surname)
            continue
        persons[surname].individual.append((at, column, note))
        persons[surname].room = room
    for at, item, room, surname, note in EQUIPMENT:
        if surname not in persons:
            missing.append(surname)
            continue
        persons[surname].equipment.append((at, item, note))
        persons[surname].room = room or persons[surname].room
    if missing:
        raise CommandError("Нет в сводках: " + ", ".join(sorted(set(missing))) + ".")
    return list(persons.values())


class Command(BaseCommand):
    help = "Текущие пациенты по сводкам постов и расписание как на planomr4.jpg (удаляет всех)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--summary",
            action="append",
            help="Сводка поста (.docx), можно несколько раз. "
            "По умолчанию — primary_docs/Svodka_*.docx.",
        )
        parser.add_argument(
            "--date",
            help="Дата расписания, ГГГГ-ММ-ДД: сегодня или следующий будний день "
            "(по умолчанию — сегодня).",
        )
        parser.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение.")

    def handle(self, *args, **options) -> None:
        dates = board_days.board_dates(board_days.today())
        day = date.fromisoformat(options["date"]) if options["date"] else board_days.today()
        if day not in dates:
            raise CommandError(
                "Расписание можно править только на сегодня и следующий будний день: "
                + ", ".join(f"{d:%d.%m.%Y}" for d in dates)
                + "."
            )
        paths = [Path(p) for p in options["summary"] or []] or sorted(
            (Path(settings.BASE_DIR) / "primary_docs").glob("Svodka_*.docx")
        )
        if not paths:
            raise CommandError("Нет сводок: положите их в primary_docs/ или укажите --summary.")
        patients: list[SummaryPatient] = []
        for path in paths:
            try:
                summary = read_summary(path)
            except (SummaryError, OSError) as error:
                raise CommandError(f"{path.name}: {error}") from error
            patients += summary.patients
            for name in summary.skipped:
                self.stdout.write(f"{path.name}: {name} — нет даты поступления, не заведён")
        persons = match(patients)

        admin = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        if admin is None:
            raise CommandError("Нужен администратор — запустите create_users.")
        count = Program.objects.count()
        if not options["yes"]:
            answer = input(
                f"Удалить всех пациентов ({count}) и завести {len(persons)} по сводкам, "
                f"расписание на {day:%d.%m.%Y}? [yes/no] "
            )
            if answer.strip().lower() not in ("yes", "y", "да"):
                raise CommandError("Отменено.")

        with transaction.atomic():
            made, skipped = self._build(admin, persons, day)
        for line in skipped:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(f"Готово: {made} пациентов, расписание на {day:%d.%m.%Y}.")
        )

    def _build(self, admin: User, persons: list[Person], day: date) -> tuple[int, list[str]]:
        department = Department.objects.get(code=DEPARTMENT_CODE)
        for program in Program.objects.all():
            programs.delete_program(admin, program)
        board_days.ensure_boards()

        slots = {f"{s.start:%-H:%M}": s for s in InstructorSlot.objects.all()}
        instructors = self._staff(admin, slots, day)
        doctors = self._doctors(department, {p.patient.doctor for p in persons})
        procedures = {p.name: p for p in Procedure.objects.all()}
        equipment = {e.name: e for e in Equipment.objects.all()}

        skipped: list[str] = []
        made: list[tuple[Person, Program]] = []
        for person in sorted(persons, key=lambda p: p.patient.admitted):
            shrm, end = self._course(department, person.patient)
            if end < day:
                skipped.append(f"{person.patient.full_name}: выписан до {day:%d.%m} — не заведён")
                continue
            program = self._program(admin, department, doctors, person, shrm, end)
            self._prescriptions(admin, program, person, procedures)
            if program.is_therapy_day(day):
                made.append((person, program))
            elif person.individual or person.equipment:
                skipped.append(
                    f"{person.patient.full_name}: {day:%d.%m} не день занятий — без расписания"
                )

        # Сначала тренажёры — на время с картинки, потом индивидуальные: иначе подбор мог
        # поставить тренажёр на время будущего индивидуального.
        self._equipment(admin, made, equipment, day)
        for person, program in made:
            for at, column, note in sorted(person.individual, key=lambda x: minutes(x[0])):
                who = self._working(instructors[column], day)
                booking = board.place_patient(admin, program, who, slots[at], day)
                if note:
                    booking.refresh_from_db()
                    board.set_note(admin, booking, booking.version, note)
        return Program.objects.count(), skipped

    def _course(self, department: Department, patient: SummaryPatient) -> tuple[int, date]:
        """ШРМ и дата выписки: из сводки, а без даты выписки — по ШРМ 4."""
        if patient.discharge is not None:
            days = (patient.discharge - patient.admitted).days + 1
            return shrm_for(days), patient.discharge
        return DEFAULT_SHRM, course_end(patient.admitted, department.course_length(DEFAULT_SHRM))

    def _program(
        self,
        admin: User,
        department: Department,
        doctors: dict[str, User],
        person: Person,
        shrm: int,
        end: date,
    ) -> Program:
        patient = person.patient
        program = Program(
            department=department,
            full_name=patient.full_name,
            sex=patient.sex,
            age=patient.age,
            history_number=patient.history_number,
            room=person.room,
            shrm=shrm,
            attending_doctor=doctors[patient.doctor],
            start_date=patient.admitted,
            end_date=end,
        )
        return programs.save_program(admin, program, end_date_changed=True)

    def _prescriptions(
        self, admin: User, program: Program, person: Person, procedures: dict[str, Procedure]
    ) -> None:
        if person.individual:
            self._prescribe(
                admin, program, procedures["Индивидуальное занятие"], len(person.individual)
            )
        for name, per_day in sorted(Counter(item for _at, item, _n in person.equipment).items()):
            self._prescribe(admin, program, procedures[name], per_day)

    def _prescribe(self, admin: User, program: Program, procedure: Procedure, per_day: int) -> None:
        programs.add_prescription(
            admin, Prescription(program=program, procedure=procedure, per_day=per_day)
        )

    def _doctors(self, department: Department, names: set[str]) -> dict[str, User]:
        """Врачи сводки — пользователи с ролью «Врач»; новые — без пароля."""
        result = {}
        for name in sorted(names):
            user = User.objects.filter(short_name=name).first()
            if user is None:
                user, created = User.objects.get_or_create(
                    username=login_for(name), defaults={"short_name": name}
                )
                if created:
                    user.set_unusable_password()
                    user.save()
                    self.stdout.write(f"Врач {name}: пользователь {user.username} без пароля")
            if not user.is_attending_doctor:
                user.is_attending_doctor = True
                user.save()
            Membership.objects.get_or_create(user=user, department=department)
            result[name] = user
        return result

    def _staff(self, admin: User, slots: dict, day: date) -> dict[str, Instructor]:
        """Пара «Паршуков/ Овсиенко» с этой даты и распорядок с картинки."""
        instructors = {i.short_name: i for i in Instructor.objects.all()}
        first, second = (instructors[name] for name in PAIR)
        left = instructors.get(LEFT)
        if left is not None and left.is_active:
            left.is_active = False
            left._history_user = admin
            left.save()
            self.stdout.write(f"{LEFT}: больше не работает (снят «работает»)")
        if first.partner_id != second.pk:
            staff.set_partner(first, second)
            second.display_order = first.display_order + 1
            second._history_user = admin
            second.save(update_fields=["display_order"])
        # Напарник 2/2 работает в дни, когда первый отдыхает: цикл сдвинут на два дня.
        own = staff.current_pattern(first, day)
        current = staff.current_pattern(second, day)
        if own is not None and (current is None or current.pattern != ShiftPatternKind.TWO_TWO):
            anchor = own.anchor_date.toordinal() + 2
            staff.change_shift_pattern(
                admin, second, ShiftPatternKind.TWO_TWO, date.fromordinal(anchor), day
            )
            staff_changes.after_pattern(admin, second, day)
            self.stdout.write(f"{second.short_name}: смена 2/2 с {day:%d.%m.%Y}")

        instructors = {i.short_name: i for i in Instructor.objects.filter(is_active=True)}
        sessions = {
            (s.procedure.name, f"{s.start_time:%-H:%M}"): s
            for s in GroupSession.objects.select_related("procedure").filter(
                procedure__department=None
            )
        }
        with_patients = {(column, at) for at, column, *_rest in INDIVIDUAL}
        for column, at in sorted(with_patients):
            for person in self._team(instructors[column]):
                if board.duty_at(person, slots[at], day) is not None:
                    board.end_duty(admin, person, slots[at], day, with_partner=False)
        for column, at, kind, group in DUTIES:
            session = sessions[(group, at)] if group else None
            for person in self._team(instructors[column]):
                current = board.duty_at(person, slots[at], day)
                if current is None or not board._same(current, kind, "", session):
                    board.add_duty(admin, person, slots[at], day, kind, group_session=session)
        for column in {column for _at, column, *_rest in INDIVIDUAL}:
            if not any(self._works(p, day) for p in self._team(instructors[column])):
                raise CommandError(f"{column}: на {day:%d.%m.%Y} никто из колонки не работает.")
        return instructors

    def _team(self, instructor: Instructor) -> list[Instructor]:
        return [instructor] + ([instructor.partner] if instructor.partner_id else [])

    def _works(self, instructor: Instructor, day: date) -> bool:
        return instructor_calendars([instructor], day, day)[instructor.pk].is_working(day)

    def _working(self, instructor: Instructor, day: date) -> Instructor:
        """Колонка пары 2/2 подписана одним из пары — ставим к работающему."""
        return next((p for p in self._team(instructor) if self._works(p, day)), instructor)

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
