import importlib
import os
import sys
from unittest.mock import patch

import pytest


_BASE_ENV_KEYS = {
    'EMAIL_HOST': '',
    'EMAIL_HOST_USER': '',
    'EMAIL_HOST_PASSWORD': '',
    'SMTP_DEBUG': '',
    'EMAIL_PORT': '',
    'EMAIL_USE_TLS': '',
    'OIDC_PROVIDER_URL': '',
    'OIDC_CLIENT_ID': '',
    'OIDC_CLIENT_SECRET': '',
    'OIDC_CLIENT_SECRET_FILE': '',
    'GOOGLE_CLIENT_ID': '',
    'GOOGLE_CLIENT_SECRET': '',
    'GOOGLE_CLIENT_SECRET_FILE': '',
}


def _reload_base(overrides=None):
    env = {**_BASE_ENV_KEYS, **(overrides or {})}
    for key in list(sys.modules):
        if key.startswith('scd_reporting.settings'):
            del sys.modules[key]
    with patch.dict(os.environ, env):
        return importlib.import_module('scd_reporting.settings.base')


class TestEmailBackendSelection:
    def test_no_email_host_uses_console_backend(self):
        mod = _reload_base({})
        assert mod.EMAIL_BACKEND == 'django.core.mail.backends.console.EmailBackend'

    def test_email_host_uses_logging_backend_by_default(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com'})
        assert mod.EMAIL_BACKEND == 'apps.core.mail.LoggingEmailBackend'

    def test_smtp_debug_flag_enables_debug_backend(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'SMTP_DEBUG': '1'})
        assert mod.EMAIL_BACKEND == 'apps.core.mail.DebugEmailBackend'

    def test_smtp_debug_zero_uses_logging_backend(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'SMTP_DEBUG': '0'})
        assert mod.EMAIL_BACKEND == 'apps.core.mail.LoggingEmailBackend'

    def test_smtp_debug_without_email_host_has_no_effect(self):
        mod = _reload_base({'SMTP_DEBUG': '1'})
        assert mod.EMAIL_BACKEND == 'django.core.mail.backends.console.EmailBackend'


class TestEmailLoggerLevel:
    def test_mail_logger_defaults_to_info(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com'})
        level = mod.LOGGING['loggers']['django.core.mail']['level']
        assert level == 'INFO'

    def test_debug_backend_still_uses_info_logger_level(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'SMTP_DEBUG': '1'})
        level = mod.LOGGING['loggers']['django.core.mail']['level']
        assert level == 'INFO'


class TestTransportSecurity:
    """Port derives the encryption mode; EMAIL_USE_TLS overrides it.

    The Fermilab gateway the OKD deployment uses is smtp.fnal.gov:25 — an
    unauthenticated relay that does offer STARTTLS — so port 25 must encrypt by
    default while remaining switchable for a relay that lacks the extension.
    """

    def test_port_465_uses_implicit_ssl(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'EMAIL_PORT': '465'})
        assert mod.EMAIL_USE_SSL is True
        assert mod.EMAIL_USE_TLS is False

    def test_port_587_negotiates_starttls(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'EMAIL_PORT': '587'})
        assert mod.EMAIL_USE_SSL is False
        assert mod.EMAIL_USE_TLS is True

    def test_port_25_negotiates_starttls_by_default(self):
        mod = _reload_base({'EMAIL_HOST': 'smtp.fnal.gov', 'EMAIL_PORT': '25'})
        assert mod.EMAIL_PORT == 25
        assert mod.EMAIL_USE_TLS is True

    def test_starttls_can_be_switched_off(self):
        mod = _reload_base({
            'EMAIL_HOST': 'relay.example.com', 'EMAIL_PORT': '25', 'EMAIL_USE_TLS': '0',
        })
        assert mod.EMAIL_USE_TLS is False
        assert mod.EMAIL_USE_SSL is False

    def test_starttls_can_be_forced_on(self):
        mod = _reload_base({
            'EMAIL_HOST': 'relay.example.com', 'EMAIL_PORT': '25', 'EMAIL_USE_TLS': '1',
        })
        assert mod.EMAIL_USE_TLS is True

    def test_use_tls_override_does_not_apply_to_implicit_ssl(self):
        mod = _reload_base({
            'EMAIL_HOST': 'smtp.example.com', 'EMAIL_PORT': '465', 'EMAIL_USE_TLS': '1',
        })
        assert mod.EMAIL_USE_SSL is True
        assert mod.EMAIL_USE_TLS is False

    def test_empty_port_falls_back_to_the_default(self):
        """The chart ships unset keys as empty strings; int('') would abort the boot."""
        mod = _reload_base({'EMAIL_HOST': 'smtp.example.com', 'EMAIL_PORT': ''})
        assert mod.EMAIL_PORT == 587

    def test_empty_username_means_no_auth_attempt(self):
        """The lab relay offers no AUTH; Django logs in only when both are set."""
        mod = _reload_base({
            'EMAIL_HOST': 'smtp.fnal.gov', 'EMAIL_PORT': '25',
            'EMAIL_HOST_PASSWORD': 'from-vault',
        })
        assert mod.EMAIL_HOST_USER == ''
        assert not (mod.EMAIL_HOST_USER and mod.EMAIL_HOST_PASSWORD)
