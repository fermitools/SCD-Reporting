from .base import *  # noqa: F401, F403

DEBUG = True

LOCAL_LOGIN_ENABLED = os.environ.get('LOCAL_LOGIN_ENABLED', '1') == '1'

MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True

SECRET_KEY = 'django-insecure-dev-only-do-not-use-in-production-abc123xyz789'

ALLOWED_HOSTS = ['*']

# Log emails to the console unless an SMTP host is explicitly configured.
# Setting EMAIL_HOST is the deliberate opt-in to real delivery, and base.py has
# already selected the SMTP backend in that case; this only covers the default.
#
# The condition matters because production currently runs these dev settings:
# helm/simple has neither whitenoise nor a proxy, so /static/ is served by the
# `if settings.DEBUG` urlpatterns in scd_reporting/urls.py. An unconditional
# override here therefore made it impossible for the deployed site to send any
# mail at all — reminders were rendered and printed into the pod log instead.
if not os.environ.get('EMAIL_HOST', '').strip():
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

SESSION_COOKIE_SAMESITE = 'Lax'

# File-based sessions avoid the UpdateError / SessionInterrupted that the
# database backend can produce when allauth's OIDC callback cycles the session
# key (deletes old DB row, inserts new one) and Django 5's session middleware
# then tries to save the now-gone original key a second time.
SESSION_ENGINE = 'django.contrib.sessions.backends.file'
