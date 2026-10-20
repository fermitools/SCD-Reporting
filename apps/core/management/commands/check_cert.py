"""Check TLS certificates and mail an alert when one is revoked, expiring or broken.

Drives the cert-monitor Helm CronJob. Command-line flags sit at the top of the
configuration hierarchy, above environment variables and config/cert_monitor.yaml.

Exit status: 0 nothing to alert on; 1 alert raised (and delivered, or delivery
not requested); 2 an alert was needed but could not be delivered, or the
configuration is unusable. A non-zero exit marks the Kubernetes Job Failed, which
is a second signal next to the mail.
"""

import socket

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand, CommandError

from apps.core import certmonitor

REMEDIATION = """\
What to do (from a checkout of SCD-Reporting, logged in with `oc login`):

  ./scripts/check-cert.sh               # same checks, plus the cert-manager side
  ./scripts/check-cert.sh --reissue     # request a new certificate
  ./scripts/check-cert.sh --recreate    # if cert-manager is in failure backoff

Background: man/check-cert.1 and the "TLS / cert-manager" section of
helm/simple/README.md. A revoked certificate stays Ready=True in cert-manager,
so it will not be replaced without one of the commands above.
"""


class Command(BaseCommand):
    help = 'Check TLS certificates (trust, name, expiry, OCSP/CRL revocation) and mail an alert on problems.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--host', action='append', dest='hosts', metavar='HOST[:PORT]',
            help='Host to check; repeat for several. Overrides CERT_MONITOR_HOSTS.',
        )
        parser.add_argument(
            '--to', action='append', dest='recipients', metavar='EMAIL',
            help='Alert recipient; repeat for several. Overrides CERT_MONITOR_RECIPIENTS.',
        )
        parser.add_argument(
            '--warn-days', type=int, metavar='DAYS',
            help='Alert when fewer days than this are left. Overrides CERT_MONITOR_WARN_DAYS.',
        )
        parser.add_argument(
            '--timeout', type=int, metavar='SECONDS',
            help='Network timeout per request. Overrides CERT_MONITOR_TIMEOUT.',
        )
        unknown = parser.add_mutually_exclusive_group()
        unknown.add_argument(
            '--alert-on-unknown', action='store_true', default=None, dest='alert_on_unknown',
            help='Alert when revocation status cannot be established.',
        )
        unknown.add_argument(
            '--ignore-unknown', action='store_false', dest='alert_on_unknown',
            help='Do not alert when revocation status cannot be established.',
        )
        parser.add_argument(
            '--no-email', action='store_true',
            help='Check and report on stdout only; never send mail.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print the alert mail instead of sending it.',
        )
        parser.add_argument(
            '--send-test', action='store_true',
            help='Send the report mail even if every check passes, to prove delivery works.',
        )

    def handle(self, *args, **options):
        hosts = options['hosts'] or settings.CERT_MONITOR_HOSTS
        recipients = options['recipients'] or settings.CERT_MONITOR_RECIPIENTS
        warn_days = options['warn_days'] if options['warn_days'] is not None else settings.CERT_MONITOR_WARN_DAYS
        timeout = options['timeout'] if options['timeout'] is not None else settings.CERT_MONITOR_TIMEOUT
        alert_on_unknown = (options['alert_on_unknown'] if options['alert_on_unknown'] is not None
                            else settings.CERT_MONITOR_ALERT_ON_UNKNOWN)
        if not hosts:
            raise CommandError('no hosts to check: set CERT_MONITOR_HOSTS or SCD_HOSTNAME, or pass --host',
                               returncode=2)

        reports = []
        for target in hosts:
            host, port = certmonitor.parse_target(target)
            report = certmonitor.check_host(host, port, warn_days=warn_days, timeout=timeout)
            reports.append(report)
            self.stdout.write(report.render())
            self.stdout.write('')

        alerting = [r for r in reports
                    if r.status == certmonitor.PROBLEM
                    or (alert_on_unknown and r.status == certmonitor.UNKNOWN)]

        if alerting or options['send_test']:
            self._notify(reports, alerting, recipients, options)

        if alerting:
            raise CommandError(
                f'certificate alert for {", ".join(r.host for r in alerting)}', returncode=1)
        self.stdout.write(self.style.SUCCESS('All certificates OK.'))

    def _notify(self, reports, alerting, recipients, options):
        if options['no_email']:
            return
        subject, body = self._compose(reports, alerting)
        if options['dry_run']:
            self.stdout.write(f'[dry-run] To: {", ".join(recipients) or "(no recipients configured)"}')
            self.stdout.write(f'[dry-run] Subject: {subject}\n\n{body}')
            return
        if not recipients:
            raise CommandError(
                'an alert is needed but no recipients are configured '
                '(CERT_MONITOR_RECIPIENTS or --to)', returncode=2)
        try:
            EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, recipients).send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001 — any SMTP failure means the alert was lost
            raise CommandError(f'alert mail to {", ".join(recipients)} failed: {exc}', returncode=2)
        self.stdout.write(f'Mailed {", ".join(recipients)}: {subject}')

    @staticmethod
    def _compose(reports, alerting):
        if alerting:
            worst = certmonitor.worst(r.status for r in alerting)
            first = alerting[0].failing()[0]
            # The detail's first clause is the finding; the rest is provenance.
            subject = (f'[SCD Reporting] TLS certificate {worst}: '
                       f'{", ".join(r.host for r in alerting)} — {first.name}: {first.detail.split(";")[0]}')
            if len(subject) > 200:
                subject = subject[:197] + '...'
        else:
            subject = '[SCD Reporting] TLS certificate monitor test: all OK'
        parts = [
            f'TLS certificate check from {socket.gethostname()}.',
            '',
            *[r.render() + '\n' for r in reports],
        ]
        if alerting:
            parts.append(REMEDIATION)
        else:
            parts.append('This is a --send-test message; no action is needed.')
        return subject, '\n'.join(parts)
