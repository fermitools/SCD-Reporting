from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    class Action(models.TextChoices):
        CREATE = 'create', 'Created'
        UPDATE = 'update', 'Updated'
        DELETE = 'delete', 'Deleted'
        LOGIN  = 'login',  'Logged in'
        LOGOUT = 'logout', 'Logged out'
        EXPORT = 'export', 'Exported'
        REMINDER = 'reminder', 'Reminder sent'

    actor       = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_log',
        db_index=True,
    )
    action      = models.CharField(max_length=16, choices=Action.choices, db_index=True)
    object_type = models.CharField(max_length=64, blank=True, db_index=True)
    object_id   = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    object_repr = models.CharField(max_length=200, blank=True)
    changes     = models.JSONField(default=dict, blank=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.CharField(max_length=500, blank=True)
    timestamp   = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['action', '-timestamp']),
            models.Index(fields=['object_type', 'object_id']),
        ]

    def __str__(self):
        actor = self.actor.email if self.actor else 'anonymous'
        return f'{self.get_action_display()} by {actor} at {self.timestamp:%Y-%m-%d %H:%M}'
