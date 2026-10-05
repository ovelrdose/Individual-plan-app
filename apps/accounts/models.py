from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.org.models import Department


class Role(models.TextChoices):
    """Роль пользователя в отделении (TZ.md §3). Администратор — флаг is_superuser."""

    DOCTOR = "doctor", "Врач"
    REHAB = "rehab", "Специалист ФР"


class User(AbstractUser):
    short_name = models.CharField(
        "фамилия и инициалы",
        max_length=60,
        blank=True,
        help_text="Печатается в карте как врач, например «Иванова А.А.» (FR-ACC-3).",
    )

    class Meta(AbstractUser.Meta):
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"

    def __str__(self) -> str:
        return self.short_name or self.get_full_name() or self.username


class Membership(models.Model):
    """Пользователь работает в отделении с ролью (FR-ACC-2)."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships", verbose_name="пользователь"
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="memberships", verbose_name="отделение"
    )
    role = models.CharField("роль", max_length=20, choices=Role.choices)

    class Meta:
        verbose_name = "членство в отделении"
        verbose_name_plural = "членство в отделениях"
        constraints = [
            models.UniqueConstraint(fields=["user", "department"], name="unique_membership"),
        ]

    def __str__(self) -> str:
        return f"{self.user} — {self.department} ({self.get_role_display()})"


class LoginFailure(models.Model):
    """Неудачная попытка входа — для блокировки подбора пароля (NFR-6)."""

    username = models.CharField(max_length=150, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "неудачный вход"
        verbose_name_plural = "неудачные входы"

    def __str__(self) -> str:
        return f"{self.username} {self.created_at:%d.%m.%Y %H:%M}"
