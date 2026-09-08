"""Fire due reminder schedules. Drives the Helm CronJob and system cron.

Command-line flags sit at the top of the configuration hierarchy, above
environment variables and config/reminders.yaml.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Send activity-report reminders for every schedule that is due.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--list', action='store_true', dest='list_only',
            help='List every schedule with its next run time and exit.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be sent without sending, logging, or advancing schedules.',
        )
        parser.add_argument(
            '--schedule', type=int, metavar='ID',
            help='Act on one schedule only, by numeric id.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='With --schedule, run it even if it is not due yet.',
        )
        parser.add_argument(
            '--recipient', metavar='EMAIL',
            help='Send a single reminder to one address, ignoring schedules. '
                 'Uses --template if given, otherwise the default template.',
        )
        parser.add_argument(
            '--template', metavar='NAME',
            help='Template name to use with --recipient.',
        )
        parser.add_argument(
            '--ignore-interval', action='store_true',
            help='With --recipient, bypass the minimum re-send interval.',
        )
        parser.add_argument(
            '--now', metavar='ISO8601',
            help='Treat this instant as the current time (testing and catch-up runs).',
        )

    def handle(self, *args, **options):
        from apps.reminders import scheduling, service
        from apps.reminders.models import ReminderSchedule, ReminderTemplate

        now = self._parse_now(options.get('now'))

        if options['list_only']:
            return self._list(ReminderSchedule, now)

        if options.get('recipient'):
            return self._one_recipient(service, ReminderTemplate, options)

        force_schedule = None
        only_pk = None
        if options.get('schedule'):
            try:
                schedule = ReminderSchedule.objects.select_related('template', 'owner').get(
                    pk=options['schedule']
                )
            except ReminderSchedule.DoesNotExist as exc:
                raise CommandError(f"No schedule with id {options['schedule']}") from exc
            if options['force']:
                force_schedule = schedule
            elif schedule.is_enabled and schedule.next_run_at and schedule.next_run_at <= now:
                only_pk = schedule.pk
            else:
                self.stdout.write(
                    f'Schedule {schedule.pk} ("{schedule.name}") is not due '
                    f'(next run {schedule.next_run_at}). Use --force to run it anyway.'
                )
                return

        results = scheduling.run_due(
            now=now, dry_run=options['dry_run'], force_schedule=force_schedule,
            only_pk=only_pk, stdout=self.stdout,
        )
        return self._report(results, options['dry_run'])

    # ── helpers ──────────────────────────────────────────────────────────────

    def _parse_now(self, raw):
        if not raw:
            return timezone.now()
        from django.utils.dateparse import parse_datetime
        parsed = parse_datetime(raw)
        if parsed is None:
            raise CommandError(f'--now must be an ISO 8601 datetime (got {raw!r})')
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed

    def _list(self, ReminderSchedule, now):
        rows = ReminderSchedule.objects.select_related('template', 'owner').order_by('pk')
        if not rows:
            self.stdout.write('No schedules configured.')
            return
        for s in rows:
            state = 'enabled' if s.is_enabled else 'disabled'
            due = 'DUE' if (s.is_enabled and s.next_run_at and s.next_run_at <= now) else '  '
            self.stdout.write(
                f'[{s.pk:>3}] {due} {s.name}  ({state})\n'
                f'        owner={s.owner} template={s.template.name}\n'
                f'        {s.cadence_display}\n'
                f'        next={s.next_run_at} last={s.last_run_at}'
            )

    def _one_recipient(self, service, ReminderTemplate, options):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        email = options['recipient']
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:
            raise CommandError(f'No user with email {email!r}') from exc

        template = None
        if options.get('template'):
            try:
                template = ReminderTemplate.objects.get(name=options['template'])
            except ReminderTemplate.DoesNotExist as exc:
                raise CommandError(f"No template named {options['template']!r}") from exc

        result = service.send_reminders(
            [user],
            template=template,
            dry_run=options['dry_run'],
            ignore_interval=options['ignore_interval'],
        )
        for _user, status, detail in result['rows']:
            self.stdout.write(f'{email}: {status}{f" — {detail}" if detail else ""}')
        return self._report([], options['dry_run'])

    def _report(self, results, dry_run):
        if not results:
            return
        sent = sum(r.get('sent', 0) for r in results)
        skipped = sum(r.get('skipped', 0) for r in results)
        failed = sum(r.get('failed', 0) for r in results)
        prefix = 'DRY RUN — would send' if dry_run else 'Sent'
        line = f'{prefix} {sent}, skipped {skipped}, failed {failed} across {len(results)} schedule(s).'
        self.stdout.write(self.style.WARNING(line) if failed else self.style.SUCCESS(line))
