"""Load application secrets from HashiCorp Vault into the process environment.

This runs once, at the very top of ``settings/base.py``, before any setting
reads ``os.environ``. Secrets fetched from Vault are injected as environment
variables, so every existing ``os.environ.get(...)`` in the settings keeps
working unchanged — nothing downstream needs to know Vault is involved.

Layout
------
Secrets live under a per-environment prefix, one KV v2 secret per service:

    okd/shared/<env>/scd-reporting/anthropic   api_key
    okd/shared/<env>/scd-reporting/django      secret_key, initial_admin_password
    okd/shared/<env>/scd-reporting/email       password
    okd/shared/<env>/scd-reporting/google      client_secret
    okd/shared/<env>/scd-reporting/oidc        client_secret
    okd/shared/<env>/scd-reporting/postgres    password

Point ``VAULT_SECRET_PATH`` at the prefix (no trailing section), e.g.
``okd/shared/prod/scd-reporting``. Sections that do not exist are skipped, so
the same code serves deployments that only populate some of them.

Configuration (all via environment)
-----------------------------------
    VAULT_ADDR          Vault server URL. Required to enable the loader.
    VAULT_SECRET_PATH   KV v2 prefix, mount included. Required to enable.
    VAULT_TOKEN         Vault token. Supplied by `deploy.sh --vault`.
    VAULT_ROLE_ID       AppRole role_id  } used only when VAULT_TOKEN is absent
    VAULT_SECRET_ID     AppRole secret_id}
    VAULT_APPROLE_MOUNT AppRole auth mount (default: auth/approle)
    VAULT_OPTIONAL      "1" to downgrade any failure to a warning

Precedence: an environment variable that is already set to a non-empty value
wins over Vault. This is deliberate — it matches the project-wide ordering of
command line > environment > config source > defaults, and it means a value
pinned in ``my-values.yaml`` still overrides Vault. The Helm chart ships those
keys as empty strings, which count as unset, so Vault fills them.
"""

import logging
import os

logger = logging.getLogger(__name__)

# section -> {key within the secret: environment variable it populates}
SECRET_MAP = {
    'anthropic': {'api_key': 'ANTHROPIC_API_KEY'},
    'django': {
        'secret_key': 'DJANGO_SECRET_KEY',
        'initial_admin_password': 'SCD_INITIAL_ADMIN_PASSWORD',
    },
    'email': {'password': 'EMAIL_HOST_PASSWORD'},
    'github': {'token': 'GITHUB_TOKEN'},
    'google': {'client_secret': 'GOOGLE_CLIENT_SECRET'},
    'oidc': {'client_secret': 'OIDC_CLIENT_SECRET'},
    'postgres': {'password': 'POSTGRES_PASSWORD'},
}

# DATABASE_URL is assembled in the ConfigMap, where the password is not
# available. Write this placeholder into it and the loader substitutes the
# value from postgres/password once Vault has been read.
DB_PASSWORD_PLACEHOLDER = '${POSTGRES_PASSWORD}'


class VaultConfigError(RuntimeError):
    """Vault was configured but could not be used."""


def _env(name, default=''):
    return os.environ.get(name, default).strip()


def _split_mount(secret_path):
    """Split 'okd/shared/prod/scd-reporting' into ('okd', 'shared/prod/scd-reporting')."""
    parts = secret_path.strip('/').split('/', 1)
    if len(parts) != 2 or not parts[1]:
        raise VaultConfigError(
            "VAULT_SECRET_PATH must include the KV mount and a path beneath it, "
            "e.g. 'okd/shared/prod/scd-reporting' (got {!r})".format(secret_path)
        )
    return parts[0], parts[1]


def _client(addr):
    try:
        import hvac
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise VaultConfigError(
            "VAULT_ADDR is set but the 'hvac' package is not installed. "
            "Add hvac to requirements.txt or unset VAULT_ADDR."
        ) from exc

    client = hvac.Client(url=addr)

    token = _env('VAULT_TOKEN')
    if token:
        client.token = token
    else:
        role_id, secret_id = _env('VAULT_ROLE_ID'), _env('VAULT_SECRET_ID')
        if not (role_id and secret_id):
            raise VaultConfigError(
                "No Vault credentials. Set VAULT_TOKEN, or both VAULT_ROLE_ID "
                "and VAULT_SECRET_ID for AppRole login."
            )
        mount = _env('VAULT_APPROLE_MOUNT', 'auth/approle')
        # hvac wants the mount without the 'auth/' prefix it is served under.
        if mount.startswith('auth/'):
            mount = mount[len('auth/'):]
        client.auth.approle.login(
            role_id=role_id, secret_id=secret_id, mount_point=mount
        )

    if not client.is_authenticated():
        raise VaultConfigError(
            "Vault rejected the supplied credentials. The application token is "
            "short-lived — redeploy with a fresh one (scripts/deploy.sh --vault)."
        )
    return client


def _read_section(client, mount, base, section):
    """Return the section's data dict, or None when the section does not exist."""
    import hvac

    try:
        resp = client.secrets.kv.v2.read_secret_version(
            path='{}/{}'.format(base, section),
            mount_point=mount,
            raise_on_deleted_version=True,
        )
    except hvac.exceptions.InvalidPath:
        return None          # section absent, or every version soft-deleted
    except hvac.exceptions.Forbidden as exc:
        raise VaultConfigError(
            "Vault denied read on {}/{}/{}. Check the token's policy.".format(
                mount, base, section)
        ) from exc
    return resp['data']['data'] or {}


def _expand_database_url():
    """Substitute the postgres password into a templated DATABASE_URL."""
    url = os.environ.get('DATABASE_URL', '')
    if DB_PASSWORD_PLACEHOLDER not in url:
        return False
    password = _env('POSTGRES_PASSWORD')
    if not password:
        raise VaultConfigError(
            "DATABASE_URL contains {} but no postgres/password was found in "
            "Vault.".format(DB_PASSWORD_PLACEHOLDER)
        )
    from urllib.parse import quote

    os.environ['DATABASE_URL'] = url.replace(
        DB_PASSWORD_PLACEHOLDER, quote(password, safe='')
    )
    return True


def load_vault_secrets():
    """Populate os.environ from Vault. No-op unless Vault is configured.

    Returns the list of environment variable names that were set.
    """
    addr, secret_path = _env('VAULT_ADDR'), _env('VAULT_SECRET_PATH')
    if not (addr and secret_path):
        return []

    optional = _env('VAULT_OPTIONAL') in ('1', 'true', 'yes')
    try:
        mount, base = _split_mount(secret_path)
        client = _client(addr)

        applied, skipped, missing = [], [], []
        for section, keys in sorted(SECRET_MAP.items()):
            data = _read_section(client, mount, base, section)
            if data is None:
                missing.append(section)
                continue
            for key, env_name in sorted(keys.items()):
                value = data.get(key)
                if value is None or value == '':
                    continue
                if _env(env_name):
                    skipped.append(env_name)     # explicit env var wins
                    continue
                os.environ[env_name] = value
                applied.append(env_name)

        if _expand_database_url():
            applied.append('DATABASE_URL')

        # Names only — never log a secret value.
        logger.info(
            'Vault %s/%s: set %s%s%s',
            mount, base,
            ', '.join(applied) or 'nothing',
            '; kept existing ' + ', '.join(skipped) if skipped else '',
            '; no section ' + ', '.join(missing) if missing else '',
        )
        return applied

    except VaultConfigError as exc:
        if optional:
            logger.warning('Vault disabled: %s', exc)
            return []
        raise
    except Exception as exc:  # network failure, TLS problem, malformed response
        if optional:
            logger.warning('Vault unreachable: %s', exc)
            return []
        raise VaultConfigError('Could not load secrets from Vault: {}'.format(exc)) from exc
