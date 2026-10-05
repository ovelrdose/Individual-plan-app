from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .admin_access import sync_admin_access
from .models import Membership, User


@receiver(post_save, sender=Membership)
@receiver(post_delete, sender=Membership)
def membership_changed(sender, instance: Membership, **kwargs) -> None:
    # Членство меняют и админка, и команды — сигнал покрывает все пути.
    user = User.objects.filter(pk=instance.user_id).first()
    if user is not None:
        sync_admin_access(user)
