from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import User


@receiver(pre_save, sender=User)
def staff_follows_superuser(sender, instance: User, **kwargs) -> None:
    """Админка Django — только у суперпользователя (TZ.md, решение 83): справочники ведутся на
    экранах сервиса. Пользователя сохраняют экран, админка, команды — сигнал покрывает все пути."""
    instance.is_staff = instance.is_superuser
