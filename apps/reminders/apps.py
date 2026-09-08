from django.apps import AppConfig


class RemindersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.reminders'
    verbose_name = 'Reminders'

    def ready(self):
        # Optional in-process scheduler. No-op unless REMINDER_SCHEDULER_ENABLED
        # is set, and never started for management commands that are not meant
        # to serve traffic (migrate, test, collectstatic, ...) — a ticker inside
        # `manage.py migrate` would try to query tables that do not exist yet.
        import sys

        from django.conf import settings

        if not getattr(settings, 'REMINDER_SCHEDULER_ENABLED', False):
            return
        argv = ' '.join(sys.argv)
        for skip in ('migrate', 'makemigrations', 'collectstatic', 'test', 'pytest',
                     'shell', 'dbshell', 'send_reminders', 'seed_admin', 'seed_taxonomy'):
            if skip in argv:
                return

        from .scheduling import start_ticker
        start_ticker()
