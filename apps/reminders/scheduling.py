"""Firing-time arithmetic and the multi-process-safe schedule runner.

Production runs several gunicorn workers, and a Helm CronJob may run alongside
them, so more than one process can decide the same schedule is due. Each due
schedule is therefore *claimed* with a single conditional UPDATE:

    UPDATE reminders_reminderschedule
       SET next_run_at = <new>, last_run_at = <now>
     WHERE id = <pk> AND next_run_at <= <now>

A statement like that is atomic on both SQLite and PostgreSQL. Exactly one
process gets a row count of 1 and goes on to send; the others get 0 and stop.
The claim is taken *before* the mail goes out, so a crash mid-batch skips that
firing rather than replaying it — for a reminder, a missed nudge is much
cheaper than a duplicated one.

All stored times are UTC. Wall-clock rules ("Friday at 15:00") are evaluated in
the schedule's own IANA timezone, so a schedule keeps its local time across DST
transitions.
"""

import calendar
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)


def _zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        from django.conf import settings
        logger.warning('reminders: unknown timezone %r, falling back to %s', name, settings.TIME_ZONE)
        return ZoneInfo(settings.TIME_ZONE)


def _localize(naive_local, tz):
    """Attach ``tz`` to a naive local datetime, tolerating DST gaps.

    A wall-clock time that does not exist on a spring-forward day (02:30 in
    America/Chicago) is pushed forward until it does.
    """
    for _ in range(4):
        aware = naive_local.replace(tzinfo=tz)
        # A wall-clock time inside the spring-forward gap comes back as a
        # different wall clock once it has been round-tripped through UTC.
        roundtrip = aware.astimezone(dt_timezone.utc).astimezone(tz)
        if roundtrip.hour == naive_local.hour and roundtrip.minute == naive_local.minute:
            return aware
        naive_local += timedelta(hours=1)
    return naive_local.replace(tzinfo=tz)


def next_fire_time(schedule, after=None):
    """Return the next UTC datetime at which ``schedule`` should fire after ``after``."""
    after = after or timezone.now()
    tz = _zone(schedule.timezone)
    local_after = after.astimezone(tz)

    if schedule.frequency == schedule.Frequency.MONTHLY:
        return _next_monthly(schedule, local_after, tz)
    return _next_weekly(schedule, local_after, tz)


def _next_weekly(schedule, local_after, tz):
    step = 14 if schedule.frequency == schedule.Frequency.BIWEEKLY else 7

    # Candidate: the target weekday of the current local week.
    days_ahead = (schedule.weekday - local_after.weekday()) % 7
    candidate_date = (local_after + timedelta(days=days_ahead)).date()
    candidate = _localize(
        datetime.combine(candidate_date, schedule.send_time), tz
    )
    if candidate <= local_after:
        candidate = _localize(
            datetime.combine(candidate_date + timedelta(days=step), schedule.send_time), tz
        )
    return candidate.astimezone(dt_timezone.utc)


def _next_monthly(schedule, local_after, tz):
    year, month = local_after.year, local_after.month
    for _ in range(13):
        day = min(schedule.day_of_month or 1, calendar.monthrange(year, month)[1])
        candidate = _localize(
            datetime.combine(local_after.date().replace(year=year, month=month, day=day),
                             schedule.send_time),
            tz,
        )
        if candidate > local_after:
            return candidate.astimezone(dt_timezone.utc)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    # Unreachable for any sane day_of_month, but never return None.
    return (local_after + timedelta(days=30)).astimezone(dt_timezone.utc)


def due_schedules(now=None):
    """Enabled schedules whose next_run_at has passed."""
    from .models import ReminderSchedule
    now = now or timezone.now()
    return ReminderSchedule.objects.filter(
        is_enabled=True,
        next_run_at__isnull=False,
        next_run_at__lte=now,
    ).select_related('template', 'owner')


def claim(schedule, now=None):
    """Atomically claim ``schedule`` for this process.

    Returns True if this process won the claim (and must now send), False if
    another process got there first.
    """
    from .models import ReminderSchedule
    now = now or timezone.now()
    advanced = next_fire_time(schedule, after=now)
    updated = (
        ReminderSchedule.objects
        .filter(pk=schedule.pk, next_run_at__lte=now)
        .update(next_run_at=advanced, last_run_at=now)
    )
    if updated:
        schedule.next_run_at = advanced
        schedule.last_run_at = now
    return bool(updated)


def run_due(now=None, dry_run=False, force_schedule=None, only_pk=None, stdout=None):
    """Fire every due schedule. Returns a list of per-schedule result dicts.

    ``force_schedule`` runs one schedule regardless of whether it is due (used
    by "Run now" and by ``send_reminders --schedule N --force``). ``only_pk``
    keeps the normal due-and-claim path but restricts it to one schedule.
    """
    from . import service

    now = now or timezone.now()
    results = []

    if force_schedule is not None:
        schedules = [force_schedule]
    else:
        qs = due_schedules(now)
        if only_pk is not None:
            qs = qs.filter(pk=only_pk)
        schedules = list(qs)
    for schedule in schedules:
        if force_schedule is None:
            if dry_run:
                # Never mutate state on a dry run; just report what would fire.
                results.append({'schedule': schedule, 'claimed': True, 'dry_run': True,
                                **service.preview_schedule(schedule)})
                continue
            if not claim(schedule, now=now):
                logger.info('reminders: schedule %s claimed by another process', schedule.pk)
                results.append({'schedule': schedule, 'claimed': False})
                continue
        elif not dry_run:
            claim_now = timezone.now()
            from .models import ReminderSchedule
            advanced = next_fire_time(schedule, after=claim_now)
            ReminderSchedule.objects.filter(pk=schedule.pk).update(
                next_run_at=advanced, last_run_at=claim_now,
            )

        outcome = service.run_schedule(schedule, dry_run=dry_run)
        outcome.update({'schedule': schedule, 'claimed': True, 'dry_run': dry_run})
        results.append(outcome)
        if stdout is not None:
            stdout.write(
                f"[{schedule.pk}] {schedule.name}: "
                f"{outcome['sent']} sent, {outcome['skipped']} skipped, {outcome['failed']} failed"
            )
    return results


# ── Optional in-process ticker ────────────────────────────────────────────────

_ticker_started = False


def start_ticker():
    """Start the background scheduler thread, at most once per process.

    Only used when REMINDER_SCHEDULER_ENABLED is on. The supported production
    driver is the Helm CronJob; this exists for single-container deployments
    that have nowhere to run cron.
    """
    global _ticker_started
    import threading

    from django.conf import settings

    if _ticker_started or not settings.REMINDER_SCHEDULER_ENABLED:
        return
    _ticker_started = True

    interval = max(15, int(settings.REMINDER_SCHEDULER_INTERVAL))

    def _loop():
        import time
        # Stagger workers so several processes don't wake in lockstep; the
        # atomic claim makes this a courtesy, not a correctness requirement.
        import random
        time.sleep(random.uniform(0, min(interval, 30)))
        while True:
            try:
                run_due()
            except Exception:  # noqa: BLE001 — a ticker must never die
                logger.exception('reminders: scheduler tick failed')
            finally:
                from django.db import connections
                # Long-lived thread: don't hold a connection open between ticks.
                connections.close_all()
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name='reminder-scheduler', daemon=True)
    thread.start()
    logger.info('reminders: in-process scheduler started (every %ss)', interval)
