"""Tests for scripts/scd_client.py: settings precedence, date ranges, parameter
encoding, and end-to-end runs of the scd-* tools against a live test server."""
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
# Interpreter for the CLI subprocesses; set SCD_CLIENT_PYTHON=/usr/bin/python3.9 (etc.)
# to check the client on another Python while the server runs on the venv's.
CLIENT_PY = os.environ.get('SCD_CLIENT_PYTHON', sys.executable)
sys.path.insert(0, str(SCRIPTS))

import scd_client  # noqa: E402
from scd_client import ScdError, load_settings, period_range  # noqa: E402


# ── Settings precedence ───────────────────────────────────────────────────────

@pytest.fixture
def cfgdir(tmp_path):
    d = tmp_path / 'home-config'
    d.mkdir()
    return d


def _settings(tmp_path, cfgdir, cli=None, env=None):
    return load_settings(cli or {}, environ=env or {}, cwd=tmp_path, home_config_dir=cfgdir)


class TestSettings:
    def test_defaults(self, tmp_path, cfgdir):
        s = _settings(tmp_path, cfgdir)
        assert s['url'] == '' and s['format'] == 'md' and s['timeout'] == 300.0
        assert s['insecure'] is False
        assert s['sources']['url'] == 'default'

    def test_full_precedence_chain(self, tmp_path, cfgdir):
        (cfgdir / 'config.yaml').write_text('url: https://from-config/\ntoken: cfgtok\nformat: csv\n'
                                            'timeout: 10\n')
        (tmp_path / '.env').write_text('SCD_TOKEN=dotenvtok\nSCD_FORMAT=txt\nOTHER=ignored\n')
        env = {'SCD_FORMAT': 'json'}
        s = _settings(tmp_path, cfgdir, cli={'timeout': '5'}, env=env)
        assert s['url'] == 'https://from-config'          # config, trailing slash stripped
        assert s['token'] == 'dotenvtok'                  # .env beats config
        assert s['format'] == 'json'                      # environment beats .env
        assert s['timeout'] == 5.0                        # command line beats everything
        assert s['sources']['timeout'] == 'command line'
        assert s['sources']['format'] == 'environment SCD_FORMAT'

    def test_cwd_dotenv_beats_home_dotenv(self, tmp_path, cfgdir):
        (cfgdir / '.env').write_text('SCD_URL=https://home\n')
        (tmp_path / '.env').write_text('export SCD_URL="https://cwd"\n')
        assert _settings(tmp_path, cfgdir)['url'] == 'https://cwd'

    def test_explicit_config_path(self, tmp_path, cfgdir):
        cfg = tmp_path / 'other.yaml'
        cfg.write_text('url: https://other\n')
        assert _settings(tmp_path, cfgdir, cli={'config': str(cfg)})['url'] == 'https://other'
        assert _settings(tmp_path, cfgdir, env={'SCD_CONFIG': str(cfg)})['url'] == 'https://other'

    def test_missing_explicit_config_is_error(self, tmp_path, cfgdir):
        with pytest.raises(ScdError):
            _settings(tmp_path, cfgdir, cli={'config': str(tmp_path / 'nope.yaml')})

    def test_token_file(self, tmp_path, cfgdir):
        tf = tmp_path / 'tok'
        tf.write_text('secret-token\n')
        s = _settings(tmp_path, cfgdir, env={'SCD_TOKEN_FILE': str(tf)})
        assert s['token'] == 'secret-token'
        # An explicit token wins over the file.
        s = _settings(tmp_path, cfgdir, cli={'token': 'cli'}, env={'SCD_TOKEN_FILE': str(tf)})
        assert s['token'] == 'cli'

    def test_insecure_parsing(self, tmp_path, cfgdir):
        assert _settings(tmp_path, cfgdir, env={'SCD_INSECURE': 'yes'})['insecure'] is True
        assert _settings(tmp_path, cfgdir, cli={'insecure': True})['insecure'] is True

    def test_bad_timeout(self, tmp_path, cfgdir):
        with pytest.raises(ScdError):
            _settings(tmp_path, cfgdir, env={'SCD_TIMEOUT': 'soon'})

    def test_yaml_fallback_parser(self, tmp_path, cfgdir, monkeypatch):
        monkeypatch.setitem(sys.modules, 'yaml', None)   # make "import yaml" fail
        (cfgdir / 'config.yaml').write_text('# comment\nurl: "https://quoted"  # trailing\n')
        assert _settings(tmp_path, cfgdir)['url'] == 'https://quoted'


# ── Periods and parameters ────────────────────────────────────────────────────

class TestPeriods:
    T = date(2026, 11, 10)   # a Tuesday in Q4

    @pytest.mark.parametrize('name,start,end', [
        ('today',        date(2026, 11, 10), date(2026, 11, 10)),
        ('this-week',    date(2026, 11, 9),  date(2026, 11, 15)),
        ('last-week',    date(2026, 11, 2),  date(2026, 11, 8)),
        ('this-month',   date(2026, 11, 1),  date(2026, 11, 30)),
        ('last-month',   date(2026, 10, 1),  date(2026, 10, 31)),
        ('this-quarter', date(2026, 10, 1),  date(2026, 12, 31)),
        ('last-quarter', date(2026, 7, 1),   date(2026, 9, 30)),
        ('this-year',    date(2026, 1, 1),   date(2026, 12, 31)),
        ('last-year',    date(2025, 1, 1),   date(2025, 12, 31)),
    ])
    def test_ranges(self, name, start, end):
        assert period_range(name, self.T) == (start, end)

    def test_year_boundaries(self):
        assert period_range('last-month', date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))
        assert period_range('last-quarter', date(2026, 2, 1)) == (date(2025, 10, 1), date(2025, 12, 31))
        assert period_range('this-month', date(2028, 2, 3))[1] == date(2028, 2, 29)

    def test_unknown(self):
        with pytest.raises(ScdError):
            period_range('fortnight')


def test_encode_params_repeats_lists_and_drops_empties():
    pairs = scd_client._encode_params({'projects': ['a', 'b'], 'x': None, 'y': '', 'z': False,
                                       'all': True, 'n': 3})
    assert pairs == [('projects', 'a'), ('projects', 'b'), ('all', 'true'), ('n', '3')]


def test_report_filters_mapping():
    p = scd_client.argparse.ArgumentParser(parents=[scd_client._report_filter_parser()])
    args = p.parse_args(['-p', 'dune', '-p', 'nova', '--period', 'last-month', '--to', '2026-10-15',
                         '--overlap', '--exclude-private', '-a', 'bob'])
    f = scd_client.report_filters(args)
    assert f['projects'] == ['dune', 'nova']
    assert f['period_before'] == '2026-10-15'          # --to overrides the period's end
    assert f['date_match'] == 'overlap'
    assert f['exclude_private'] is True
    assert f['author_email'] == 'bob'
    assert 'is_private' not in f and 'show_archived' not in f


def test_client_requires_url_and_token():
    with pytest.raises(ScdError):
        scd_client.Client('', 'tok')
    with pytest.raises(ScdError):
        scd_client.Client('https://x', '')


def test_main_dispatch_usage():
    assert scd_client.main([]) == scd_client.EXIT_USAGE
    assert scd_client.main(['bogus']) == scd_client.EXIT_USAGE


# ── End to end against a live server ──────────────────────────────────────────

@pytest.fixture
def live(live_server, transactional_db, tmp_path):
    from apps.accounts.models import APIToken, User
    from apps.entries.models import WorkItem
    from apps.taxonomy.models import Category, Project

    dune = Project.objects.create(name='DUNE', slug='dune')
    nova = Project.objects.create(name='NOvA', slug='nova')
    sci = Category.objects.create(name='Scientific', slug='scientific')
    auditor = User.objects.create_user(username='aud', email='aud@example.com', password='x',
                                       role=User.Role.AUDITOR)
    user = User.objects.create_user(username='u', email='u@example.com', password='x')
    for author, title, proj in ((user, 'Timing fix', dune), (user, 'DAQ upgrade', nova),
                                (auditor, 'Audit notes', dune)):
        e = WorkItem.objects.create(author=author, title=title, description=f'{title} details',
                                    period_kind='week', period_start=date(2026, 10, 5),
                                    period_end=date(2026, 10, 11))
        e.projects.set([proj])
        e.categories.set([sci])
    # Isolate the tools from the developer's real config and .env files.
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('SCD_')}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'SCD_URL': live_server.url})
    return {
        'env': env, 'cwd': tmp_path,
        'auditor': APIToken.rotate(auditor).key,
        'user': APIToken.rotate(user).key,
    }


def _run(live, tool, *args, token='auditor'):
    env = dict(live['env'], SCD_TOKEN=live[token])
    return subprocess.run([CLIENT_PY, str(SCRIPTS / tool), *args], env=env, cwd=live['cwd'],
                          capture_output=True, timeout=60)


class TestEndToEnd:
    def test_report_markdown(self, live):
        r = _run(live, 'scd-report', '-p', 'dune', '--from', '2026-10-01', '--to', '2026-10-31')
        assert r.returncode == 0, r.stderr
        out = r.stdout.decode()
        assert '## Timing fix' in out and '## Audit notes' in out and 'DAQ upgrade' not in out

    def test_report_count_and_json(self, live):
        assert _run(live, 'scd-report', '--count').stdout.strip() == b'3'
        r = _run(live, 'scd-report', '-f', 'json', '-s', 'DAQ')
        assert [e['title'] for e in json.loads(r.stdout)] == ['DAQ upgrade']

    def test_report_to_file(self, live):
        r = _run(live, 'scd-report', '-f', 'xlsx', '-o', 'out.xlsx')
        assert r.returncode == 0, r.stderr
        assert (live['cwd'] / 'out.xlsx').read_bytes()[:2] == b'PK'

    def test_report_remote_name(self, live):
        r = _run(live, 'scd-report', '-f', 'csv', '-O')
        assert r.returncode == 0, r.stderr
        assert list(live['cwd'].glob('scd_report_*.csv'))

    def test_report_forbidden_for_regular_user(self, live):
        r = _run(live, 'scd-report', token='user')
        assert r.returncode == scd_client.EXIT_API
        assert b'HTTP 403' in r.stderr

    def test_report_unknown_slug(self, live):
        r = _run(live, 'scd-report', '-p', 'atlas')
        assert r.returncode == scd_client.EXIT_API
        assert b'projects' in r.stderr and b'atlas' in r.stderr

    def test_bad_token(self, live):
        env = dict(live['env'], SCD_TOKEN='wrong')
        r = subprocess.run([CLIENT_PY, str(SCRIPTS / 'scd-whoami')], env=env,
                           cwd=live['cwd'], capture_output=True, timeout=60)
        assert r.returncode == scd_client.EXIT_API
        assert b'HTTP 401' in r.stderr

    def test_missing_token_is_usage_error(self, live):
        r = subprocess.run([CLIENT_PY, str(SCRIPTS / 'scd-whoami')], env=live['env'],
                           cwd=live['cwd'], capture_output=True, timeout=60)
        assert r.returncode == scd_client.EXIT_USAGE
        assert b'No API token' in r.stderr

    def test_unreachable_server(self, live):
        env = dict(live['env'], SCD_TOKEN='x', SCD_URL='http://127.0.0.1:9')
        r = subprocess.run([CLIENT_PY, str(SCRIPTS / 'scd-whoami')], env=env,
                           cwd=live['cwd'], capture_output=True, timeout=60)
        assert r.returncode == scd_client.EXIT_NETWORK

    def test_whoami(self, live):
        r = _run(live, 'scd-whoami')
        assert r.returncode == 0, r.stderr
        assert b'Auditor' in r.stdout and b'aud@example.com' in r.stdout
        r = _run(live, 'scd-whoami', '--settings')
        assert b'environment SCD_URL' in r.stdout
        assert live['auditor'].encode() not in r.stdout   # token is masked

    def test_taxonomy(self, live):
        r = _run(live, 'scd-taxonomy', 'projects')
        assert r.returncode == 0, r.stderr
        assert b'dune' in r.stdout and b'nova' in r.stdout
        data = json.loads(_run(live, 'scd-taxonomy', '--json').stdout)
        assert 'tags' in data and 'categories' in data

    def test_entries_table_and_export(self, live):
        r = _run(live, 'scd-entries', token='user')
        assert r.returncode == 0, r.stderr
        assert b'Timing fix' in r.stdout and b'Audit notes' not in r.stdout
        md = _run(live, 'scd-entries', '-p', 'nova', '-f', 'md', token='user').stdout.decode()
        assert md.startswith('## DAQ upgrade')

    def test_entry_detail(self, live):
        entries = json.loads(_run(live, 'scd-entries', '--json', token='user').stdout)
        eid = str(entries[0]['id'])
        r = _run(live, 'scd-entry', eid, token='user')
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith(b'## ')
        assert json.loads(_run(live, 'scd-entry', eid, '-f', 'json', token='user').stdout)['id'] == int(eid)

    def test_module_dispatch(self, live):
        env = dict(live['env'], SCD_TOKEN=live['auditor'])
        r = subprocess.run([CLIENT_PY, str(SCRIPTS / 'scd_client.py'), 'report', '--count'],
                           env=env, cwd=live['cwd'], capture_output=True, timeout=60)
        assert r.stdout.strip() == b'3'

    def test_summary_without_api_key_reports_server_error(self, live, settings):
        # The live server has no Anthropic key in tests, so the server returns its
        # configuration error as a 400 and the tool exits with EXIT_API.
        settings.ANTHROPIC_API_KEY = ''
        r = _run(live, 'scd-summary', '-p', 'dune')
        assert r.returncode == scd_client.EXIT_API
        assert b'HTTP 400' in r.stderr
