"""Who may a given lead remind?

Mirrors the report scoping in ``apps.reports.views`` (``_get_group_scope`` /
``_get_project_scope``) so a lead can only mail the people whose entries they
can already read:

    admin, role preview  every active user
    division head        users whose primary group is one they manage
    group leader         users in their own primary group
    functional lead      authors of entries tagged with a project they manage
    anything else        nobody

``auditor`` is intentionally absent. It is a read-only role, and sending mail on
someone else's behalf is a write action.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def can_send(user) -> bool:
    """True if ``user`` may open the reminders pages and send reminders."""
    if not getattr(user, 'is_authenticated', False):
        return False
    return bool(
        user.is_scd_admin
        or user.is_division_head
        or user.is_group_leader
        or user.is_functional_lead
    )


def recipient_qs(user):
    """Return the ``User`` queryset ``user`` is allowed to remind."""
    if not can_send(user):
        return User.objects.none()

    base = User.objects.filter(is_active=True).exclude(email='')

    # Admins previewing another role have no real managed groups or projects, so
    # scope restrictions would leave the page empty and untestable.
    if user.is_scd_admin or getattr(user, '_is_previewing', False):
        return base

    if user.is_division_head:
        return base.filter(group__in=user.managed_groups.all())

    if user.is_group_leader:
        if not user.group_id:
            return User.objects.none()
        return base.filter(group_id=user.group_id)

    if user.is_functional_lead:
        projects = user.managed_projects.all()
        if not projects.exists():
            return User.objects.none()
        # A functional lead's people are those who have booked effort against
        # one of their projects, either on the entry itself or through the
        # entry's group falling inside the project. Only the former exists in
        # the data model, so match on the entry's projects.
        return base.filter(work_items__projects__in=projects).distinct()

    return User.objects.none()


def scope_description(user) -> str:
    """Human-readable summary of the scope, for the page header."""
    if user.is_scd_admin or getattr(user, '_is_previewing', False):
        return 'All active users'
    if user.is_division_head:
        names = list(user.managed_groups.values_list('name', flat=True))
        return 'Groups you manage: ' + (', '.join(names) if names else 'none assigned')
    if user.is_group_leader:
        return f'Your group: {user.group.name}' if user.group_id else 'No group assigned'
    if user.is_functional_lead:
        names = list(user.managed_projects.values_list('name', flat=True))
        return 'Projects you lead: ' + (', '.join(names) if names else 'none assigned')
    return 'No scope'


def in_scope(user, recipient) -> bool:
    """True if ``recipient`` is inside ``user``'s scope. Used to gate single sends."""
    return recipient_qs(user).filter(pk=recipient.pk).exists()


def visible_schedules(user):
    """Schedules ``user`` may see and edit: their own, or all of them for an admin."""
    from .models import ReminderSchedule
    qs = ReminderSchedule.objects.select_related('template', 'owner')
    if user.is_scd_admin:
        return qs
    return qs.filter(owner=user)


def visible_log(user):
    """Reminder log rows inside ``user``'s scope."""
    from .models import ReminderLog
    qs = ReminderLog.objects.select_related('recipient', 'template', 'schedule', 'sent_by')
    if user.is_scd_admin:
        return qs
    return qs.filter(Q(recipient__in=recipient_qs(user)) | Q(sent_by=user)).distinct()
