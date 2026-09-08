"""Tests for the YAML application-config layer (scd_reporting/appconfig.py).

The contract is the project-wide ordering — command line > environment > .env >
config file > default — of which this module implements the bottom three, plus
the rule that a broken or missing config file must never break settings import.
"""
import pytest

from scd_reporting import appconfig


@pytest.fixture(autouse=True)
def _clear_cache():
    appconfig._FILE_CACHE.clear()
    yield
    appconfig._FILE_CACHE.clear()


def _write(tmp_path, monkeypatch, text, name='reminders.yaml'):
    (tmp_path / name).write_text(text)
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    return name


def test_missing_file_yields_an_empty_section(tmp_path, monkeypatch):
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    assert appconfig.section('nope.yaml', 'reminders') == {}


def test_unparsable_file_is_ignored(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, 'reminders: [unclosed\n')
    assert appconfig.section(name, 'reminders') == {}


def test_non_mapping_file_is_ignored(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, '- one\n- two\n')
    assert appconfig.section(name, 'reminders') == {}


def test_section_is_read(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, 'reminders:\n  stale_days: 21\n')
    assert appconfig.section(name, 'reminders') == {'stale_days': 21}


def test_yaml_beats_the_default(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, 'reminders:\n  stale_days: 21\n')
    data = appconfig.section(name, 'reminders')
    assert appconfig.resolve(data, 'stale_days', 'REMINDER_STALE_DAYS', 14, cast=int) == 21


def test_environment_beats_yaml(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, 'reminders:\n  stale_days: 21\n')
    monkeypatch.setenv('REMINDER_STALE_DAYS', '7')
    data = appconfig.section(name, 'reminders')
    assert appconfig.resolve(data, 'stale_days', 'REMINDER_STALE_DAYS', 14, cast=int) == 7


def test_empty_environment_variable_counts_as_unset(tmp_path, monkeypatch):
    """The Helm chart ships keys as empty strings; those must not win."""
    name = _write(tmp_path, monkeypatch, 'reminders:\n  base_url: "https://from.yaml"\n')
    monkeypatch.setenv('REMINDER_BASE_URL', '   ')
    data = appconfig.section(name, 'reminders')
    assert appconfig.resolve(data, 'base_url', 'REMINDER_BASE_URL', '') == 'https://from.yaml'


def test_default_is_used_when_nothing_else_supplies_a_value(tmp_path, monkeypatch):
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    assert appconfig.resolve({}, 'stale_days', 'REMINDER_STALE_DAYS', 14, cast=int) == 14


def test_bad_integer_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('REMINDER_STALE_DAYS', 'lots')
    assert appconfig.resolve({}, 'stale_days', 'REMINDER_STALE_DAYS', 14, cast=int) == 14


@pytest.mark.parametrize('raw,expected', [
    ('1', True), ('true', True), ('TRUE', True), ('yes', True), ('on', True),
    ('0', False), ('false', False), ('no', False), ('off', False),
])
def test_boolean_spellings(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('REMINDER_SCHEDULER_ENABLED', raw)
    got = appconfig.resolve({}, 'scheduler_enabled', 'REMINDER_SCHEDULER_ENABLED',
                            False, cast=bool)
    assert got is expected


def test_yaml_native_boolean_is_honoured(tmp_path, monkeypatch):
    name = _write(tmp_path, monkeypatch, 'reminders:\n  scheduler_enabled: true\n')
    data = appconfig.section(name, 'reminders')
    got = appconfig.resolve(data, 'scheduler_enabled', 'REMINDER_SCHEDULER_ENABLED',
                            False, cast=bool)
    assert got is True


def test_shipped_example_parses(tmp_path, monkeypatch):
    """config/reminders.yaml.example must be valid and cover every setting."""
    import shutil
    from pathlib import Path
    shutil.copy(Path('config/reminders.yaml.example'), tmp_path / 'reminders.yaml')
    monkeypatch.setenv('SCD_CONFIG_DIR', str(tmp_path))
    data = appconfig.section('reminders.yaml', 'reminders')
    assert set(data) == {
        'base_url', 'min_interval_hours', 'stale_days', 'batch_size',
        'scheduler_enabled', 'scheduler_interval',
    }
