"""Reminder rendering and delivery.

Rendering is a plain ``{{token}}`` substitution rather than a Django template
render: the body is operator-supplied text, and handing it to the template
engine would turn the edit box into an arbitrary-code surface. Unknown tokens
are left verbatim so a typo is visible in the sent mail rather than silently
blanking a line.

The HTML alternative goes through ``apps.core.markdown.render_markdown``, which
sanitises with nh3 — the same path the entry descriptions take.
"""

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r'\{\{\s*(\w+)\s*\}\}')

# Documented on the template edit page.
PLACEHOLDERS = [
    ('recipient_name',        "The recipient's display name, or their email address."),
    ('recipient_email',       "The recipient's email address."),
    ('recipient_group',       "The recipient's primary group, or 'unassigned'."),
    ('last_entry_date',       "Period end of their most recent entry, or 'never'."),
    ('last_entry_title',      'Title of their most recent entry, or an empty string.'),
    ('last_entry_summary',    "One-line summary: title plus date, or 'no entries on record yet'."),
    ('days_since_last_entry', "Whole days since that entry, or 'n/a'."),
    ('entry_count',           'How many entries they have submitted in total.'),
    ('report_url',            'Link to the reporting interface.'),
    ('new_entry_url',         'Link straight to the new-entry form.'),
    ('my_entries_url',        "Link to the recipient's own entry list."),
    ('sender_name',           'The lead who sent the reminder, or the schedule name.'),
    ('today',                 "Today's date, ISO format."),
]


# ── Recipient facts ───────────────────────────────────────────────────────────

def last_entry_for(user):
    """The recipient's most recent non-archived entry, or None."""
    from apps.entries.models import WorkItem
    return (
        WorkItem.objects
        .filter(author=user, is_archived=False)
        .order_by('-period_end', '-created_at')
        .first()
    )


def entry_count_for(user):
    from apps.entries.models import WorkItem
    return WorkItem.objects.filter(author=user, is_archived=False).count()


def recipient_facts(user):
    """Everything the recipient table and the placeholder substitution need."""
    entry = last_entry_for(user)
    today = timezone.localdate()
    days = (today - entry.period_end).days if entry else None
    return {
        'user': user,
        'last_entry': entry,
        'last_entry_at': entry.period_end if entry else None,
        'days_since': days,
        'entry_count': entry_count_for(user),
    }


def annotate_freshness(qs, stale_days):
    """Add reporting-freshness aggregates to a recipient queryset.

    One query instead of two per person: the page has to summarise the whole
    scope (the "N overdue" count and the stale-only filter) before it
    paginates, so a per-user round trip would scale with the size of the
    division rather than the size of the page.
    """
    from django.db.models import Count, Max, Q as _Q

    active = _Q(work_items__is_archived=False)
    return qs.annotate(
        last_entry_end=Max('work_items__period_end', filter=active),
        entry_total=Count('work_items', filter=active, distinct=True),
    )


def stale_q(stale_days):
    """Queryset filter matching people with no entry newer than ``stale_days``.

    Mirrors is_stale() in SQL so the overdue count and the stale-only filter
    stay a COUNT and a WHERE rather than a Python loop.
    """
    from django.db.models import Q as _Q

    cutoff = timezone.localdate() - timedelta(days=stale_days)
    return _Q(last_entry_end__isnull=True) | _Q(last_entry_end__lte=cutoff)


def rows_from_annotated(users, stale_days):
    """Build display rows for one page of annotated users.

    Two bulk queries for the whole page — the title of each person's latest
    entry, and their latest reminder — instead of two per row.
    """
    from apps.entries.models import WorkItem

    from .models import ReminderLog

    users = list(users)
    today = timezone.localdate()
    ids = [u.pk for u in users]

    # Latest entry per author, for the title column. Ordering puts the newest
    # first, so the first write per author into the dict is the one kept.
    titles = {}
    if ids:
        for item in (WorkItem.objects
                     .filter(author_id__in=ids, is_archived=False)
                     .order_by('author_id', '-period_end', '-created_at')
                     .only('author_id', 'title', 'period_end')):
            titles.setdefault(item.author_id, item)

    reminders = {}
    if ids:
        for log in (ReminderLog.objects
                    .filter(recipient_id__in=ids, status=ReminderLog.Status.SENT)
                    .order_by('recipient_id', '-created_at')):
            reminders.setdefault(log.recipient_id, log)

    rows = []
    for user in users:
        last_end = getattr(user, 'last_entry_end', None)
        rows.append({
            'user': user,
            'last_entry': titles.get(user.pk),
            'last_entry_at': last_end,
            'days_since': (today - last_end).days if last_end else None,
            'entry_count': getattr(user, 'entry_total', 0),
            'stale': last_end is None or (today - last_end).days >= stale_days,
            'last_reminder': reminders.get(user.pk),
        })
    return rows


def is_stale(facts, stale_days):
    """True when the recipient has no entry, or none newer than ``stale_days``."""
    if facts['last_entry_at'] is None:
        return True
    return facts['days_since'] is not None and facts['days_since'] >= stale_days


# ── Rendering ─────────────────────────────────────────────────────────────────

def base_url(request=None):
    """Absolute site root for links inside the email.

    Configured value wins, because a scheduled send has no request and because
    the request Host on a proxied deployment is not always the public name.
    """
    configured = (getattr(settings, 'REMINDER_BASE_URL', '') or '').rstrip('/')
    if configured:
        return configured
    if request is not None:
        return request.build_absolute_uri('/').rstrip('/')
    return ''


def placeholder_values(user, facts=None, sender_name='', request=None, root=None):
    facts = facts or recipient_facts(user)
    root = root if root is not None else base_url(request)
    entry = facts['last_entry']

    if entry is None:
        last_date = 'never'
        last_title = ''
        summary = 'no entries on record yet'
        days = 'n/a'
    else:
        last_date = entry.period_end.isoformat()
        last_title = entry.title
        summary = f'"{entry.title}" for the period ending {last_date}'
        days = str(facts['days_since'])

    return {
        'recipient_name':        user.display_name or user.email or user.username,
        'recipient_email':       user.email,
        'recipient_group':       user.group.name if user.group_id else 'unassigned',
        'last_entry_date':       last_date,
        'last_entry_title':      last_title,
        'last_entry_summary':    summary,
        'days_since_last_entry': days,
        'entry_count':           str(facts['entry_count']),
        'report_url':            f"{root}{reverse('dashboard')}",
        'new_entry_url':         f"{root}{reverse('entries:create')}",
        'my_entries_url':        f"{root}{reverse('entries:list')}",
        'sender_name':           sender_name or 'SCD Activity Reporting System',
        'today':                 timezone.localdate().isoformat(),
    }


def substitute(text, values):
    """Replace ``{{token}}`` with its value, leaving unknown tokens untouched."""
    def _sub(match):
        key = match.group(1)
        return values.get(key, match.group(0))
    return _TOKEN_RE.sub(_sub, text or '')


def render_reminder(template, user, facts=None, sender_name='', request=None, root=None):
    """Return ``(subject, text_body, html_body)`` for one recipient."""
    from apps.core.markdown import render_markdown

    values = placeholder_values(user, facts=facts, sender_name=sender_name,
                                request=request, root=root)
    subject = substitute(template.subject, values).strip() or 'Activity report reminder'
    # Header injection guard: a subject must be a single line.
    subject = ' '.join(subject.splitlines())[:200]
    text = substitute(template.body, values)
    html = render_markdown(text)
    return subject, text, html


# ── Delivery ──────────────────────────────────────────────────────────────────

def _recently_reminded(user, hours):
    from .models import ReminderLog
    if hours <= 0:
        return None
    cutoff = timezone.now() - timedelta(hours=hours)
    return (
        ReminderLog.objects
        .filter(recipient=user, status=ReminderLog.Status.SENT, created_at__gte=cutoff)
        .order_by('-created_at')
        .first()
    )


def send_reminders(
    recipients,
    template=None,
    *,
    sent_by=None,
    schedule=None,
    request=None,
    sender_name='',
    only_stale=False,
    stale_days=None,
    ignore_interval=False,
    dry_run=False,
):
    """Send one reminder per recipient.

    Returns ``{'sent': int, 'skipped': int, 'failed': int, 'rows': [...]}`` where
    each row is ``(user, status, detail)``. Every attempt is written to
    ``ReminderLog`` unless ``dry_run`` is set.

    Recipients without an email address, recipients that are not stale (when
    ``only_stale``), and recipients reminded inside REMINDER_MIN_INTERVAL_HOURS
    are skipped rather than failed — a skip is a normal outcome here.
    """
    from .models import ReminderLog, ReminderTemplate

    template = template or ReminderTemplate.get_default()
    stale_days = settings.REMINDER_STALE_DAYS if stale_days is None else stale_days
    interval = 0 if ignore_interval else int(settings.REMINDER_MIN_INTERVAL_HOURS)
    root = base_url(request)
    if not sender_name:
        if sent_by is not None:
            sender_name = sent_by.display_name or sent_by.email
        elif schedule is not None:
            sender_name = schedule.name

    recipients = list(recipients)[:int(settings.REMINDER_BATCH_SIZE)]
    rows = []
    counts = {'sent': 0, 'skipped': 0, 'failed': 0}
    logs = []
    messages = []

    for user in recipients:
        facts = recipient_facts(user)

        if not user.email:
            rows.append((user, 'skipped', 'no email address on file'))
            counts['skipped'] += 1
            continue

        if only_stale and not is_stale(facts, stale_days):
            rows.append((user, 'skipped', f'entry within the last {stale_days} days'))
            counts['skipped'] += 1
            continue

        recent = _recently_reminded(user, interval)
        if recent is not None:
            detail = f'already reminded {recent.created_at:%Y-%m-%d %H:%M}'
            rows.append((user, 'skipped', detail))
            counts['skipped'] += 1
            logs.append(ReminderLog(
                recipient=user, to_email=user.email, subject='', template=template,
                schedule=schedule, sent_by=sent_by, status=ReminderLog.Status.SKIPPED,
                detail=detail[:500], last_entry_at=facts['last_entry_at'],
            ))
            continue

        subject, text, html = render_reminder(
            template, user, facts=facts, sender_name=sender_name, request=request, root=root,
        )

        if dry_run:
            rows.append((user, 'sent', 'dry run — not delivered'))
            counts['sent'] += 1
            continue

        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
            reply_to=[sent_by.email] if sent_by is not None and sent_by.email else None,
        )
        message.attach_alternative(html, 'text/html')
        messages.append((user, facts, subject, message))

    # One SMTP connection for the whole batch.
    if messages:
        connection = get_connection(fail_silently=False)
        try:
            connection.open()
        except Exception as exc:  # noqa: BLE001 — report, don't crash the request
            logger.error('reminders: could not open mail connection: %s', exc)
            for user, facts, subject, _msg in messages:
                rows.append((user, 'failed', str(exc)[:200]))
                counts['failed'] += 1
                logs.append(ReminderLog(
                    recipient=user, to_email=user.email, subject=subject, template=template,
                    schedule=schedule, sent_by=sent_by, status=ReminderLog.Status.FAILED,
                    detail=str(exc)[:500], last_entry_at=facts['last_entry_at'],
                ))
            messages = []
        else:
            for user, facts, subject, message in messages:
                message.connection = connection
                try:
                    message.send()
                except Exception as exc:  # noqa: BLE001 — one bad address must not stop the batch
                    logger.error('reminders: send to %s failed: %s', user.email, exc)
                    rows.append((user, 'failed', str(exc)[:200]))
                    counts['failed'] += 1
                    status, detail = ReminderLog.Status.FAILED, str(exc)[:500]
                else:
                    rows.append((user, 'sent', ''))
                    counts['sent'] += 1
                    status, detail = ReminderLog.Status.SENT, ''
                logs.append(ReminderLog(
                    recipient=user, to_email=user.email, subject=subject, template=template,
                    schedule=schedule, sent_by=sent_by, status=status, detail=detail,
                    last_entry_at=facts['last_entry_at'],
                ))
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    if logs and not dry_run:
        ReminderLog.objects.bulk_create(logs)

    if not dry_run and (counts['sent'] or counts['failed']):
        from apps.audit.service import log_event
        log_event(
            action='reminder',
            actor=sent_by,
            obj=schedule or template,
            changes={
                'sent': counts['sent'],
                'skipped': counts['skipped'],
                'failed': counts['failed'],
                'template': template.name,
                'schedule': schedule.name if schedule else None,
            },
            request=request,
        )

    return {**counts, 'rows': rows, 'template': template}


# ── Schedule execution ────────────────────────────────────────────────────────

def schedule_recipients(schedule):
    """Recipients for a schedule, resolved from its owner's scope."""
    from . import scope
    return scope.recipient_qs(schedule.owner).select_related('group').order_by('email')


def run_schedule(schedule, dry_run=False):
    """Send one schedule's reminders. Called only by a process that claimed it."""
    return send_reminders(
        schedule_recipients(schedule),
        template=schedule.template,
        schedule=schedule,
        sent_by=None,
        sender_name=schedule.name,
        only_stale=schedule.only_stale,
        stale_days=schedule.stale_days,
        dry_run=dry_run,
    )


def preview_schedule(schedule):
    """Count what a schedule would do, without sending or logging anything."""
    recipients = list(schedule_recipients(schedule))
    would_send = 0
    for user in recipients:
        if not user.email:
            continue
        if schedule.only_stale and not is_stale(recipient_facts(user), schedule.stale_days):
            continue
        would_send += 1
    return {
        'sent': would_send,
        'skipped': len(recipients) - would_send,
        'failed': 0,
        'rows': [],
    }
