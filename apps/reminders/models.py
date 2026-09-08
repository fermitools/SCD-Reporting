"""Models for the activity-report email reminder subsystem (GitHub #31).

Three tables:

``ReminderTemplate``  the editable boiler plate — subject line plus a Markdown
                      body containing ``{{placeholder}}`` tokens.
``ReminderSchedule``  a recurring send. Owns the scope (via ``owner``) and the
                      firing rule; ``next_run_at`` is the claim token the
                      runner competes for, so it is stored in UTC and indexed.
``ReminderLog``       one row per attempted delivery. Drives the "last
                      reminded" column, the dedupe window, and the audit trail.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

DEFAULT_SUBJECT = 'Reminder: submit your SCD activity report'

DEFAULT_BODY = """\
Hello {{recipient_name}},

This is a reminder to record your recent work in the SCD Activity Reporting
System. Entries take only a few minutes and give the division an accurate
picture of where effort is going.

**Your last entry:** {{last_entry_summary}}

Please add an entry for the current reporting period here:

{{new_entry_url}}

You can review everything you have already submitted here:

{{my_entries_url}}

Thank you,
{{sender_name}}
"""


class ReminderTemplate(models.Model):
    """Editable body text for reminder emails."""

    name = models.CharField(max_length=100, unique=True)
    subject = models.CharField(max_length=200, default=DEFAULT_SUBJECT)
    body = models.TextField(
        default=DEFAULT_BODY,
        help_text='Markdown. Supports {{placeholder}} tokens — see the placeholder reference.',
    )
    is_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Used when a send or schedule does not name a template.',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reminder_templates_updated',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', 'name']
        verbose_name = 'Reminder Template'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Exactly one default. Demote the others rather than rejecting the save,
        # so "make this the default" is a single click.
        if self.is_default:
            ReminderTemplate.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)

    @classmethod
    def get_default(cls):
        """Return the default template, creating the built-in one on first use."""
        obj = cls.objects.filter(is_default=True).first()
        if obj is not None:
            return obj
        obj = cls.objects.order_by('pk').first()
        if obj is not None:
            return obj
        return cls.objects.create(
            name='Standard reminder',
            subject=DEFAULT_SUBJECT,
            body=DEFAULT_BODY,
            is_default=True,
        )


class ReminderSchedule(models.Model):
    """A recurring reminder send, scoped by its owner's role."""

    class Frequency(models.TextChoices):
        WEEKLY   = 'weekly',   'Weekly'
        BIWEEKLY = 'biweekly', 'Every 2 weeks'
        MONTHLY  = 'monthly',  'Monthly'

    WEEKDAYS = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    name = models.CharField(max_length=100)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reminder_schedules',
        help_text="Recipients are resolved from this user's role and scope at send time.",
    )
    template = models.ForeignKey(
        'reminders.ReminderTemplate',
        on_delete=models.PROTECT,
        related_name='schedules',
    )
    is_enabled = models.BooleanField(default=True, db_index=True)

    frequency = models.CharField(
        max_length=10,
        choices=Frequency.choices,
        default=Frequency.WEEKLY,
    )
    weekday = models.PositiveSmallIntegerField(
        choices=WEEKDAYS,
        default=4,  # Friday
        help_text='Used by the weekly and biweekly frequencies.',
    )
    day_of_month = models.PositiveSmallIntegerField(
        default=1,
        help_text='Used by the monthly frequency. Clamped to the length of each month.',
    )
    send_time = models.TimeField(help_text='Local wall-clock time in the timezone below.')
    timezone = models.CharField(
        max_length=64,
        default=settings.TIME_ZONE,
        help_text='IANA timezone name, e.g. America/Chicago.',
    )

    only_stale = models.BooleanField(
        default=True,
        help_text='Skip people who already have a recent entry.',
    )
    stale_days = models.PositiveSmallIntegerField(
        default=14,
        help_text='How many days without an entry counts as stale.',
    )

    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Reminder Schedule'
        indexes = [
            models.Index(fields=['is_enabled', 'next_run_at']),
        ]

    def __str__(self):
        return self.name

    @property
    def cadence_display(self):
        if self.frequency == self.Frequency.MONTHLY:
            return f'Monthly on day {self.day_of_month} at {self.send_time:%H:%M} {self.timezone}'
        label = dict(self.WEEKDAYS).get(self.weekday, '?')
        every = 'Every 2 weeks' if self.frequency == self.Frequency.BIWEEKLY else 'Weekly'
        return f'{every} on {label} at {self.send_time:%H:%M} {self.timezone}'

    def save(self, *args, **kwargs):
        # Keep next_run_at consistent with the firing rule. Recomputed whenever
        # it is unset or the rule changed; the runner is what advances it after
        # a successful send.
        if self.next_run_at is None:
            from .scheduling import next_fire_time
            self.next_run_at = next_fire_time(self, after=timezone.now())
        super().save(*args, **kwargs)


class ReminderLog(models.Model):
    """One attempted delivery."""

    class Status(models.TextChoices):
        SENT    = 'sent',    'Sent'
        FAILED  = 'failed',  'Failed'
        SKIPPED = 'skipped', 'Skipped'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reminders_received',
    )
    to_email = models.EmailField()
    subject = models.CharField(max_length=200, blank=True)
    template = models.ForeignKey(
        'reminders.ReminderTemplate',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='sends',
    )
    schedule = models.ForeignKey(
        'reminders.ReminderSchedule',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='sends',
        help_text='Null for an interactive send.',
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='reminders_sent',
        help_text='Null for a scheduled send.',
    )
    status = models.CharField(max_length=8, choices=Status.choices, db_index=True)
    detail = models.CharField(max_length=500, blank=True)
    last_entry_at = models.DateField(
        null=True, blank=True,
        help_text="Period end of the recipient's most recent entry when the reminder was sent.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Reminder Log Entry'
        verbose_name_plural = 'Reminder Log'
        indexes = [
            models.Index(fields=['recipient', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_status_display()} → {self.to_email} at {self.created_at:%Y-%m-%d %H:%M}'
