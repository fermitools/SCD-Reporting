"""YAML application configuration with environment-variable overrides.

Secrets come from Vault (``scd_reporting/vault.py``); *operational* settings —
things an operator may want to change without rebuilding the image — come from
YAML files under ``config/`` and can be overridden per-deployment by an
environment variable.

Resolution order, highest priority first:

    1. command line          (handled by each management command's own flags)
    2. environment variable  (includes anything python-dotenv loaded from .env)
    3. YAML file             (``config/<name>.yaml``)
    4. hard-coded default

The loader is deliberately forgiving: a missing file, an unparsable file, or a
PyYAML that is not installed all degrade to "no YAML layer", leaving the
environment variables and defaults in charge. Nothing here may raise during
``settings`` import.

Configuration
-------------
    SCD_CONFIG_DIR      Directory holding the YAML files.
                        Default: ``<repo root>/config``.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent

_FILE_CACHE: dict[Path, dict] = {}


def config_dir() -> Path:
    override = os.environ.get('SCD_CONFIG_DIR', '').strip()
    return Path(override) if override else _BASE_DIR / 'config'


def load_yaml(filename: str) -> dict:
    """Return the parsed mapping from ``config/<filename>``, or ``{}``."""
    path = config_dir() / filename
    if path in _FILE_CACHE:
        return _FILE_CACHE[path]

    data: dict = {}
    if path.is_file():
        try:
            import yaml  # PyYAML is an optional dependency at import time
        except ImportError:
            logger.warning(
                'appconfig: %s exists but PyYAML is not installed; '
                'falling back to environment variables and defaults', path
            )
        else:
            try:
                loaded = yaml.safe_load(path.read_text()) or {}
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    logger.warning('appconfig: %s does not contain a mapping; ignoring', path)
            except Exception as exc:  # noqa: BLE001 — settings import must not fail
                logger.warning('appconfig: could not parse %s: %s', path, exc)

    _FILE_CACHE[path] = data
    return data


def section(filename: str, name: str) -> dict:
    """Return one top-level mapping from a YAML file, or ``{}``."""
    value = load_yaml(filename).get(name)
    return value if isinstance(value, dict) else {}


def as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    return default


def as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def resolve(data: dict, key: str, env_var: str, default, cast=str):
    """Resolve one setting: environment variable, then YAML, then default.

    ``cast`` is applied to whichever layer supplied the value. An environment
    variable set to the empty string counts as unset, which matches the Helm
    chart's habit of shipping keys as empty strings.
    """
    raw = os.environ.get(env_var, '').strip()
    if raw:
        source = raw
    elif key in data and data[key] not in (None, ''):
        source = data[key]
    else:
        return default

    if cast is bool:
        return as_bool(source, default)
    if cast is int:
        return as_int(source, default)
    return cast(source)
