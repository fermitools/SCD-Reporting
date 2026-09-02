import os
from pathlib import Path

import dj_database_url

from scd_reporting.vault import load_vault_secrets

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Pull secrets from Vault into os.environ before anything below reads them.
# No-op unless VAULT_ADDR and VAULT_SECRET_PATH are both set, so local and
# docker-compose runs are unaffected. Environment variables that are already
# set win over Vault — see scd_reporting/vault.py.
load_vault_secrets()

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-only-change-in-production')

# GitHub credentials for bug report submission.
# Preferred: GitHub App installation auth (all three vars required).
#   GITHUB_APP_ID            — numeric App ID from the App's settings page
#   GITHUB_APP_INSTALLATION_ID — installation ID for the target repo
#   GITHUB_APP_PRIVATE_KEY   — PEM private key content (newlines as \n or literal)
# Fallback: personal access token with 'public_repo' scope.
GITHUB_APP_ID             = os.environ.get('GITHUB_APP_ID', '')
GITHUB_APP_INSTALLATION_ID = os.environ.get('GITHUB_APP_INSTALLATION_ID', '')
_raw_pem = os.environ.get('GITHUB_APP_PRIVATE_KEY', '')
GITHUB_APP_PRIVATE_KEY    = _raw_pem.replace('\\n', '\n') if _raw_pem else ''
GITHUB_TOKEN              = os.environ.get('GITHUB_TOKEN', '')

# Largest taxonomy JSON file the admin import page will read into memory.
TAXONOMY_IMPORT_MAX_BYTES = int(os.environ.get('TAXONOMY_IMPORT_MAX_BYTES', 5 * 1024 * 1024))

DEBUG = False

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost').split(',')]

# Trust X-Forwarded-Proto from the TLS-terminating reverse proxy (OKD router, Caddy, etc.)
# Safe in all environments — only activates when the header is present.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# CSRF trusted origins — required when Django is behind an HTTPS reverse proxy.
# Set CSRF_TRUSTED_ORIGINS explicitly, or derive from SCD_HOSTNAME.
_csrf_env = os.environ.get('CSRF_TRUSTED_ORIGINS', '').strip()
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(',')]
elif os.environ.get('SCD_HOSTNAME', '').strip():
    CSRF_TRUSTED_ORIGINS = [f"https://{os.environ['SCD_HOSTNAME'].strip()}"]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'django.contrib.sites',
    # Third-party
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.openid_connect',
    'allauth.socialaccount.providers.google',
    'allauth.mfa',
    'allauth.mfa.webauthn',
    'django_htmx',
    'django_filters',
    'tailwind',
    'widget_tweaks',
    # Local
    'theme',
    'apps.core.apps.CoreConfig',
    'apps.accounts.apps.AccountsConfig',
    'apps.taxonomy.apps.TaxonomyConfig',
    'apps.entries.apps.EntriesConfig',
    'apps.reports.apps.ReportsConfig',
    'apps.audit.apps.AuditConfig',
]

AUTH_USER_MODEL = 'accounts.User'

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'apps.audit.middleware.AuditRequestMiddleware',
    'apps.accounts.middleware.RolePreviewMiddleware',
    'apps.accounts.middleware.GroupSelectionMiddleware',
]

ROOT_URLCONF = 'scd_reporting.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'apps' / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.accounts.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'scd_reporting.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# django-allauth — local auth
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = os.environ.get('ACCOUNT_EMAIL_VERIFICATION', 'optional')
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# SSO seam — flip to '1' to disable local signup once OIDC is configured
SCD_DISABLE_LOCAL_SIGNUP = os.environ.get('SCD_DISABLE_LOCAL_SIGNUP', '0') == '1'

# Show email/password form on the login page. Off by default so production
# deployments that use SSO exclusively don't expose the local login form.
# Override to True in dev.py or via LOCAL_LOGIN_ENABLED=1 env var.
LOCAL_LOGIN_ENABLED = os.environ.get('LOCAL_LOGIN_ENABLED', '0') == '1'

ACCOUNT_ADAPTER = 'apps.accounts.adapters.AccountAdapter'
SOCIALACCOUNT_ADAPTER = 'apps.accounts.adapters.SocialAccountAdapter'

# ── OIDC / SSO ────────────────────────────────────────────────────────────────
# Client secret: read from a file (preferred for production) or env var fallback.
#   OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_secret   (contents = raw secret)
#   OIDC_CLIENT_SECRET=<value>                         (direct env var fallback)
#
# Discovery URL: either the full .well-known URL or the base realm URL.
#   OIDC_PROVIDER_URL=https://host/realms/myrealm/.well-known/openid-configuration
#   OIDC_PROVIDER_URL=https://host/realms/myrealm
#
# Client ID:
#   OIDC_CLIENT_ID=scd-report-summarizer

def _read_oidc_secret():
    path = os.environ.get('OIDC_CLIENT_SECRET_FILE', '').strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError as exc:
            from django.core.exceptions import ImproperlyConfigured
            raise ImproperlyConfigured(
                f"OIDC_CLIENT_SECRET_FILE={path!r} is set but cannot be read: {exc}"
            ) from exc
    return os.environ.get('OIDC_CLIENT_SECRET', '')

_OIDC_PROVIDER_URL = os.environ.get('OIDC_PROVIDER_URL', '').strip()
# Accept full discovery URL or base realm URL — allauth wants the base URL
_OIDC_SERVER_URL = (
    _OIDC_PROVIDER_URL.removesuffix('/.well-known/openid-configuration')
    if _OIDC_PROVIDER_URL else ''
)
OIDC_CLIENT_ID = os.environ.get('OIDC_CLIENT_ID', '').strip()
_OIDC_CLIENT_SECRET = _read_oidc_secret()
OIDC_ENABLED = bool(_OIDC_SERVER_URL and OIDC_CLIENT_ID and _OIDC_CLIENT_SECRET)

# ── Google OAuth ──────────────────────────────────────────────────────────────
# GOOGLE_CLIENT_ID=<your-client-id>
# GOOGLE_CLIENT_SECRET=<your-client-secret>  (or GOOGLE_CLIENT_SECRET_FILE=<path>)

def _read_google_secret():
    path = os.environ.get('GOOGLE_CLIENT_SECRET_FILE', '').strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    return os.environ.get('GOOGLE_CLIENT_SECRET', '')

_GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
_GOOGLE_CLIENT_SECRET = _read_google_secret()
GOOGLE_ENABLED = bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)

SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

_SOCIALACCOUNT_PROVIDERS: dict = {}

if OIDC_ENABLED:
    _SOCIALACCOUNT_PROVIDERS['openid_connect'] = {
        'APPS': [
            {
                'provider_id': 'keycloak',
                'name': 'Fermilab SSO',
                'client_id': OIDC_CLIENT_ID,
                'secret': _OIDC_CLIENT_SECRET,
                'settings': {
                    'server_url': _OIDC_SERVER_URL,
                },
            }
        ]
    }

if GOOGLE_ENABLED:
    _SOCIALACCOUNT_PROVIDERS['google'] = {
        'APPS': [
            {
                'provider_id': 'google',
                'name': 'Google',
                'client_id': _GOOGLE_CLIENT_ID,
                'secret': _GOOGLE_CLIENT_SECRET,
                'settings': {
                    'scope': ['profile', 'email'],
                    'auth_params': {'access_type': 'online'},
                },
            }
        ]
    }

if _SOCIALACCOUNT_PROVIDERS:
    SOCIALACCOUNT_PROVIDERS = _SOCIALACCOUNT_PROVIDERS

# ── Passkeys / WebAuthn ───────────────────────────────────────────────────────
MFA_SUPPORTED_TYPES = ['webauthn']
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_ALLOW_UNVERIFIED_EMAIL = True
# Defaults to False (secure). Override with MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN=true
# only for local development (localhost without HTTPS).
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = os.environ.get(
    'MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN', 'false'
).lower() == 'true'

# ── Email ─────────────────────────────────────────────────────────────────────
# Set EMAIL_HOST to enable SMTP sending.  All settings read from env vars:
#   EMAIL_HOST          smtp host (required to enable SMTP)
#   EMAIL_PORT          587 = STARTTLS (default), 465 = implicit SSL
#   EMAIL_HOST_USER     SMTP username
#   EMAIL_HOST_PASSWORD SMTP password
#   DEFAULT_FROM_EMAIL  sender address shown to recipients
#   SMTP_DEBUG          set to "1" to enable full SMTP protocol tracing
#                       (development/diagnostics only — do not use in production)
_email_host = os.environ.get('EMAIL_HOST', '').strip()
if _email_host:
    _smtp_debug = os.environ.get('SMTP_DEBUG', '').strip() == '1'
    EMAIL_BACKEND = (
        'apps.core.mail.DebugEmailBackend'
        if _smtp_debug
        else 'apps.core.mail.LoggingEmailBackend'
    )
    EMAIL_HOST = _email_host
    EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
    # Port 465 = implicit SSL; anything else = STARTTLS
    if EMAIL_PORT == 465:
        EMAIL_USE_SSL = True
        EMAIL_USE_TLS = False
    else:
        EMAIL_USE_SSL = False
        EMAIL_USE_TLS = True
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@localhost')

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_DIR = BASE_DIR / 'logs'
_LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(_LOG_DIR / 'scd_reporting.log'),
            'maxBytes': 10 * 1024 * 1024,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'django.core.mail': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        # Capture our own app logs
        'apps': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

TAILWIND_APP_NAME = 'theme'
INTERNAL_IPS = ['127.0.0.1']

# Anthropic — used for AI report summaries
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_SUMMARY_MODEL = os.environ.get('ANTHROPIC_SUMMARY_MODEL', 'claude-sonnet-4-6')
ANTHROPIC_BASE_URL = os.environ.get('ANTHROPIC_BASE_URL', '')
