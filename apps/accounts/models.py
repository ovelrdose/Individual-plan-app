from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.org.models import Department


class Right(models.TextChoices):
    """Что пользователю можно (TZ.md §3, решение 79). Права общие для всех его отделений.

    Видеть программы своих отделений может каждый; суперпользователь может всё.
    """

    PROGRAMS_EDIT = "programs_edit", "Программы: создавать и править, загружать листы назначений"
    PROGRAMS_DELETE = "programs_delete", "Программы: удалять"
    SCHEDULE = "schedule", "Расписание пациентов: индивидуальные, группы и тренажёры"
    GROUP_SCHEDULE = "group_schedule", "Групповые занятия: расписание групп"
    STAFF = "staff", "Инструкторы: смены и распорядок"
    CATALOG = "catalog", "Справочники: процедуры, тренажёры, инструкторы"
    USERS = "users", "Пользователи и отделения (видит все отделения)"


# Короткие подписи — для списка пользователей.
RIGHT_SHORT = {
    Right.PROGRAMS_EDIT: "программы",
    Right.PROGRAMS_DELETE: "удаление программ",
    Right.SCHEDULE: "расписание",
    Right.GROUP_SCHEDULE: "группы",
    Right.STAFF: "смены",
    Right.CATALOG: "справочники",
    Right.USERS: "пользователи",
}


class Preset(models.TextChoices):
    """Шаблон прав на экране пользователя: только проставляет галочки (решение 79)."""

    DOCTOR = "doctor", "Врач"
    REHAB = "rehab", "Специалист ФР"
    ADMIN = "admin", "Администратор"


PRESET_RIGHTS: dict[Preset, tuple[Right, ...]] = {
    Preset.DOCTOR: (Right.PROGRAMS_EDIT,),
    Preset.REHAB: (
        Right.PROGRAMS_EDIT,
        Right.PROGRAMS_DELETE,
        Right.SCHEDULE,
        Right.GROUP_SCHEDULE,
        Right.STAFF,
        Right.CATALOG,
    ),
    Preset.ADMIN: tuple(Right),
}
# Шаблон «Врач» ещё и делает пользователя лечащим врачом.
PRESET_ATTENDING = {Preset.DOCTOR: True, Preset.REHAB: False, Preset.ADMIN: False}


class User(AbstractUser):
    short_name = models.CharField(
        "фамилия и инициалы",
        max_length=60,
        blank=True,
        help_text="Печатается в карте как врач, например «Иванова А.А.» (FR-ACC-3).",
    )
    is_attending_doctor = models.BooleanField(
        "лечащий врач",
        default=False,
        help_text="Можно выбрать лечащим врачом программы; печатается в карте.",
    )
    rights = ArrayField(
        models.CharField(max_length=30, choices=Right.choices), default=list, blank=True
    )
    departments = models.ManyToManyField(
        Department, through="Membership", related_name="users", verbose_name="отделения"
    )

    class Meta(AbstractUser.Meta):
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"

    def __str__(self) -> str:
        return self.short_name or self.get_full_name() or self.username

    def apply_preset(self, preset: Preset) -> None:
        """Проставляет права шаблона (не сохраняет)."""
        self.rights = [right.value for right in PRESET_RIGHTS[preset]]
        self.is_attending_doctor = PRESET_ATTENDING[preset]


class Membership(models.Model):
    """Пользователь работает в отделении и видит его программы (FR-ACC-2)."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships", verbose_name="пользователь"
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="memberships", verbose_name="отделение"
    )

    class Meta:
        verbose_name = "членство в отделении"
        verbose_name_plural = "членство в отделениях"
        constraints = [
            models.UniqueConstraint(fields=["user", "department"], name="unique_membership"),
        ]

    def __str__(self) -> str:
        return f"{self.user} — {self.department}"


class LoginFailure(models.Model):
    """Неудачная попытка входа — для блокировки подбора пароля (NFR-6)."""

    username = models.CharField(max_length=150, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "неудачный вход"
        verbose_name_plural = "неудачные входы"

    def __str__(self) -> str:
        return f"{self.username} {self.created_at:%d.%m.%Y %H:%M}"
