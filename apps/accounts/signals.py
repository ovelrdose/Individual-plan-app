from django.db.models.signals import pre_save
from django.dispatch import receiver

from .admin_access import needs_admin
from .models import User


@receiver(pre_save, sender=User)
def staff_follows_rights(sender, instance: User, **kwargs) -> None:
    # Пользователя сохраняют экран, админка, команды — сигнал покрывает все пути.
    instance.is_staff = needs_admin(instance)
