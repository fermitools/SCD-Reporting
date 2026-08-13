"""Tests for the Vault -> environment secret loader.

These never touch a real Vault server; the hvac client is stubbed out.
"""

import logging

import pytest

from scd_reporting import vault
from scd_reporting.vault import VaultConfigError, load_vault_secrets

PATH = 'okd/shared/test/scd-reporting'
ADDR = 'https://vault.example:8200'

SECRETS = {
    'anthropic': {'api_key': 'anthropic-key'},
    'django': {'secret_key': 'django-key', 'initial_admin_password': 'admin-pw'},
    'email': {'password': 'email-pw'},
    'google': {'client_secret': 'google-secret'},
    'oidc': {'client_secret': 'oidc-secret'},
    'postgres': {'password': 'pg-pw'},
}


@pytest.fixture
def vault_env(monkeypatch):
    """Configure the loader and clear every variable it can populate."""
    monkeypatch.setenv('VAULT_ADDR', ADDR)
    monkeypatch.setenv('VAULT_SECRET_PATH', PATH)
    monkeypatch.setenv('VAULT_TOKEN', 'test-token')
    monkeypatch.delenv('VAULT_OPTIONAL', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    for keys in vault.SECRET_MAP.values():
        for env_name in keys.values():
            monkeypatch.delenv(env_name, raising=False)
    return monkeypatch


@pytest.fixture
def fake_vault(monkeypatch):
    """Stub _client/_read_section. Returns the mutable secret store."""
    store = {k: dict(v) for k, v in SECRETS.items()}
    monkeypatch.setattr(vault, '_client', lambda addr: object())
    monkeypatch.setattr(
        vault, '_read_section',
        lambda client, mount, base, section: store.get(section),
    )
    return store


# ── Enablement ────────────────────────────────────────────────────────────────

def test_noop_when_not_configured(monkeypatch):
    monkeypatch.delenv('VAULT_ADDR', raising=False)
    monkeypatch.delenv('VAULT_SECRET_PATH', raising=False)
    assert load_vault_secrets() == []


def test_noop_when_only_addr_set(monkeypatch):
    monkeypatch.setenv('VAULT_ADDR', ADDR)
    monkeypatch.delenv('VAULT_SECRET_PATH', raising=False)
    assert load_vault_secrets() == []


# ── Core mapping ──────────────────────────────────────────────────────────────

def test_populates_all_mapped_env_vars(vault_env, fake_vault, monkeypatch):
    applied = load_vault_secrets()
    import os
    assert os.environ['DJANGO_SECRET_KEY'] == 'django-key'
    assert os.environ['SCD_INITIAL_ADMIN_PASSWORD'] == 'admin-pw'
    assert os.environ['ANTHROPIC_API_KEY'] == 'anthropic-key'
    assert os.environ['EMAIL_HOST_PASSWORD'] == 'email-pw'
    assert os.environ['GOOGLE_CLIENT_SECRET'] == 'google-secret'
    assert os.environ['OIDC_CLIENT_SECRET'] == 'oidc-secret'
    assert os.environ['POSTGRES_PASSWORD'] == 'pg-pw'
    assert set(applied) == {
        'DJANGO_SECRET_KEY', 'SCD_INITIAL_ADMIN_PASSWORD', 'ANTHROPIC_API_KEY',
        'EMAIL_HOST_PASSWORD', 'GOOGLE_CLIENT_SECRET', 'OIDC_CLIENT_SECRET',
        'POSTGRES_PASSWORD',
    }


def test_empty_env_var_is_overwritten(vault_env, fake_vault):
    """The Helm chart ships these keys as "" — Vault must win over that."""
    import os
    vault_env.setenv('DJANGO_SECRET_KEY', '')
    vault_env.setenv('OIDC_CLIENT_SECRET', '   ')
    load_vault_secrets()
    assert os.environ['DJANGO_SECRET_KEY'] == 'django-key'
    assert os.environ['OIDC_CLIENT_SECRET'] == 'oidc-secret'


def test_existing_env_var_wins(vault_env, fake_vault):
    """An operator override in my-values.yaml must not be clobbered."""
    import os
    vault_env.setenv('DJANGO_SECRET_KEY', 'pinned-by-operator')
    applied = load_vault_secrets()
    assert os.environ['DJANGO_SECRET_KEY'] == 'pinned-by-operator'
    assert 'DJANGO_SECRET_KEY' not in applied
    assert os.environ['OIDC_CLIENT_SECRET'] == 'oidc-secret'


def test_missing_section_is_tolerated(vault_env, fake_vault):
    """scd-reporting has no github/ section; that must not be an error."""
    import os
    del fake_vault['oidc']
    applied = load_vault_secrets()
    assert 'OIDC_CLIENT_SECRET' not in applied
    assert os.environ.get('OIDC_CLIENT_SECRET') is None
    assert os.environ['DJANGO_SECRET_KEY'] == 'django-key'


def test_empty_value_in_vault_is_skipped(vault_env, fake_vault):
    import os
    fake_vault['django']['secret_key'] = ''
    applied = load_vault_secrets()
    assert 'DJANGO_SECRET_KEY' not in applied
    assert os.environ.get('DJANGO_SECRET_KEY') is None


# ── DATABASE_URL templating ───────────────────────────────────────────────────

def test_database_url_placeholder_expanded(vault_env, fake_vault):
    import os
    vault_env.setenv('DATABASE_URL', 'postgres://scd:${POSTGRES_PASSWORD}@db:5432/scd')
    applied = load_vault_secrets()
    assert os.environ['DATABASE_URL'] == 'postgres://scd:pg-pw@db:5432/scd'
    assert 'DATABASE_URL' in applied


def test_database_url_password_is_url_encoded(vault_env, fake_vault):
    import os
    fake_vault['postgres']['password'] = 'p@ss/w:rd'
    vault_env.setenv('DATABASE_URL', 'postgres://scd:${POSTGRES_PASSWORD}@db:5432/scd')
    load_vault_secrets()
    assert os.environ['DATABASE_URL'] == 'postgres://scd:p%40ss%2Fw%3Ard@db:5432/scd'


def test_database_url_without_placeholder_untouched(vault_env, fake_vault):
    import os
    vault_env.setenv('DATABASE_URL', 'sqlite:////app/data/db.sqlite3')
    load_vault_secrets()
    assert os.environ['DATABASE_URL'] == 'sqlite:////app/data/db.sqlite3'


def test_placeholder_without_vault_password_raises(vault_env, fake_vault):
    del fake_vault['postgres']
    vault_env.setenv('DATABASE_URL', 'postgres://scd:${POSTGRES_PASSWORD}@db:5432/scd')
    with pytest.raises(VaultConfigError, match='POSTGRES_PASSWORD'):
        load_vault_secrets()


# ── Failure handling ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('bad_path', ['okd', '/okd/', ''])
def test_malformed_secret_path_raises(vault_env, fake_vault, bad_path):
    vault_env.setenv('VAULT_SECRET_PATH', bad_path)
    if bad_path == '':
        assert load_vault_secrets() == []      # unset disables the loader
    else:
        with pytest.raises(VaultConfigError, match='VAULT_SECRET_PATH'):
            load_vault_secrets()


def test_failure_is_fatal_by_default(vault_env, monkeypatch):
    def boom(addr):
        raise OSError('connection refused')
    monkeypatch.setattr(vault, '_client', boom)
    with pytest.raises(VaultConfigError, match='Could not load secrets'):
        load_vault_secrets()


def test_vault_optional_downgrades_failure(vault_env, monkeypatch, caplog):
    def boom(addr):
        raise OSError('connection refused')
    monkeypatch.setattr(vault, '_client', boom)
    vault_env.setenv('VAULT_OPTIONAL', '1')
    with caplog.at_level(logging.WARNING):
        assert load_vault_secrets() == []
    assert 'Vault unreachable' in caplog.text


# ── Secret hygiene ────────────────────────────────────────────────────────────

def test_secret_values_are_never_logged(vault_env, fake_vault, caplog):
    with caplog.at_level(logging.DEBUG):
        load_vault_secrets()
    for section in SECRETS.values():
        for value in section.values():
            assert value not in caplog.text
    assert 'DJANGO_SECRET_KEY' in caplog.text     # names are fine
