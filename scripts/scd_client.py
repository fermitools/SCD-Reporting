#!/usr/bin/env python3
"""scd_client — Python client and command-line tools for the SCD Reporting token API.

Library use::

    from scd_client import Client, load_settings
    c = Client.from_settings(load_settings())
    md = c.report('md', projects=['dune'], period_after='2026-10-01')

Command-line use (each tool is also a sub-command of this module)::

    scd-whoami
    scd-taxonomy projects
    scd-entries --period last-month
    scd-entry 123 -f md
    scd-report --project dune --period this-month -f md -o oct.md
    scd-summary --project dune --period last-week

    python scd_client.py report -f md        # same thing, e.g. on Windows

Settings precedence: command line > environment > .env > config file > default.
Only the Python standard library is required (PyYAML is used if present).
Compatible with Python 3.9+.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

__version__ = '1.0.0'

DEFAULT_CONFIG_DIR = Path.home() / '.config' / 'scd-reporting'
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / 'config.yaml'

# setting name -> (environment variable, default)
SETTINGS = {
    'url':        ('SCD_URL',        ''),
    'token':      ('SCD_TOKEN',      ''),
    'token_file': ('SCD_TOKEN_FILE', ''),
    'timeout':    ('SCD_TIMEOUT',    '300'),
    'ca_bundle':  ('SCD_CA_BUNDLE',  ''),
    'insecure':   ('SCD_INSECURE',   'false'),
    'format':     ('SCD_FORMAT',     'md'),
}

BINARY_FORMATS = ('xlsx', 'pdf')
PERIODS = ('today', 'this-week', 'last-week', 'this-month', 'last-month',
           'this-quarter', 'last-quarter', 'this-year', 'last-year')
TAXONOMY_KINDS = ('projects', 'categories', 'entry-types', 'groups', 'lab-priorities', 'tags')

EXIT_OK, EXIT_API, EXIT_USAGE, EXIT_NETWORK = 0, 1, 2, 3


# ── Errors ────────────────────────────────────────────────────────────────────

class ScdError(Exception):
    """Configuration or usage problem on the client side."""


class ScdApiError(Exception):
    """The server answered with a non-2xx status."""

    def __init__(self, status, message, errors=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or {}

    def __str__(self):
        lines = [f'HTTP {self.status}: {self.message}']
        lines += [f'  {k}: {v}' for k, v in self.errors.items()]
        return '\n'.join(lines)


class ScdNetworkError(Exception):
    """The server could not be reached."""


# ── Settings ──────────────────────────────────────────────────────────────────

def _load_yaml(path):
    text = Path(path).read_text(encoding='utf-8')
    try:
        import yaml
    except ImportError:
        data = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or ':' not in line:
                continue
            k, _, v = line.partition(':')
            data[k.strip()] = v.split(' #')[0].strip().strip('"\'')
        return data
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ScdError(f'{path}: top level must be a mapping')
    return data


def _load_dotenv(path):
    data = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):]
        k, _, v = line.partition('=')
        data[k.strip()] = v.strip().strip('"\'')
    return data


def _truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def load_settings(cli=None, environ=None, cwd=None, home_config_dir=None):
    """Resolve client settings.

    Precedence (highest first): ``cli`` dict > ``environ`` > ``.env`` files
    (``./.env`` then ``~/.config/scd-reporting/.env``) > YAML config file
    (``--config``, else ``$SCD_CONFIG``, else ``~/.config/scd-reporting/config.yaml``)
    > defaults. Returns a dict with keys from SETTINGS plus ``sources`` that
    records where each value came from (useful for ``scd-whoami --settings``).
    """
    cli = {k: v for k, v in (cli or {}).items() if v not in (None, '')}
    environ = os.environ if environ is None else environ
    cwd = Path(cwd or os.getcwd())
    cfg_dir = Path(home_config_dir) if home_config_dir else DEFAULT_CONFIG_DIR

    dotenv = {}
    for path in (cfg_dir / '.env', cwd / '.env'):   # later file wins
        if path.is_file():
            dotenv.update({k: v for k, v in _load_dotenv(path).items() if k.startswith('SCD_')})

    config_path = (cli.get('config') or environ.get('SCD_CONFIG')
                   or dotenv.get('SCD_CONFIG') or str(cfg_dir / 'config.yaml'))
    config = {}
    if Path(config_path).is_file():
        config = _load_yaml(config_path)
    elif cli.get('config') or environ.get('SCD_CONFIG'):
        raise ScdError(f'Config file not found: {config_path}')

    out, sources = {}, {}
    for name, (env, default) in SETTINGS.items():
        for label, value in (('command line', cli.get(name)),
                             (f'environment {env}', environ.get(env)),
                             (f'.env {env}', dotenv.get(env)),
                             (f'config {config_path}', config.get(name)),
                             ('default', default)):
            if value not in (None, ''):
                out[name], sources[name] = value, label
                break
        else:
            out[name], sources[name] = default, 'default'

    if not out['token'] and out['token_file']:
        tf = Path(os.path.expanduser(str(out['token_file'])))
        if not tf.is_file():
            raise ScdError(f'token_file not found: {tf}')
        out['token'] = tf.read_text(encoding='utf-8').strip()
        sources['token'] = f'token_file {tf}'
    out['url'] = str(out['url']).rstrip('/')
    try:
        out['timeout'] = float(out['timeout'])
    except ValueError:
        raise ScdError(f'timeout must be a number, got {out["timeout"]!r}')
    out['insecure'] = out['insecure'] is True or _truthy(out['insecure'])
    out['config_path'] = config_path
    out['sources'] = sources
    return out


# ── Dates ─────────────────────────────────────────────────────────────────────

def period_range(name, today=None):
    """Return (start, end) dates for a named period. Weeks start on Monday."""
    t = today or date.today()
    if name == 'today':
        return t, t
    if name in ('this-week', 'last-week'):
        start = t - timedelta(days=t.weekday())
        if name == 'last-week':
            start -= timedelta(days=7)
        return start, start + timedelta(days=6)
    if name in ('this-month', 'last-month'):
        y, m = t.year, t.month
        if name == 'last-month':
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    if name in ('this-quarter', 'last-quarter'):
        q = (t.month - 1) // 3
        y = t.year
        if name == 'last-quarter':
            y, q = (y - 1, 3) if q == 0 else (y, q - 1)
        m0 = 3 * q + 1
        return date(y, m0, 1), date(y, m0 + 2, calendar.monthrange(y, m0 + 2)[1])
    if name in ('this-year', 'last-year'):
        y = t.year - (name == 'last-year')
        return date(y, 1, 1), date(y, 12, 31)
    raise ScdError(f'Unknown period "{name}". Choose from: {", ".join(PERIODS)}')


# ── HTTP client ───────────────────────────────────────────────────────────────

class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def content_type(self):
        return self.headers.get('Content-Type', '')

    @property
    def filename(self):
        m = re.search(r'filename="([^"]+)"', self.headers.get('Content-Disposition', ''))
        return os.path.basename(m.group(1)) if m else None

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def text(self):
        return self.body.decode('utf-8')


def _encode_params(params):
    """dict -> list of (key, value); lists repeat the key, None/empty/False drop out."""
    pairs = []
    for key, value in (params or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if v is None or v == '' or v is False:
                continue
            if v is True:
                v = 'true'
            pairs.append((key, str(v)))
    return pairs


class Client:
    """Minimal client for the SCD Reporting ``/api/`` endpoints."""

    def __init__(self, url, token, timeout=300, ca_bundle=None, insecure=False, verbose=False):
        if not url:
            raise ScdError('No server URL. Set --url, SCD_URL, or "url:" in the config file.')
        if not token:
            raise ScdError('No API token. Set --token, SCD_TOKEN, or "token:" / "token_file:" '
                           'in the config file. Generate one at Profile > API Token.')
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.verbose = verbose
        if insecure:
            self._ssl = ssl._create_unverified_context()
        elif ca_bundle:
            self._ssl = ssl.create_default_context(cafile=os.path.expanduser(ca_bundle))
        else:
            self._ssl = ssl.create_default_context()

    @classmethod
    def from_settings(cls, settings, verbose=False):
        return cls(settings['url'], settings['token'], timeout=settings['timeout'],
                   ca_bundle=settings['ca_bundle'] or None, insecure=settings['insecure'],
                   verbose=verbose)

    # -- transport ------------------------------------------------------------

    def request(self, method, path, params=None, json_body=None):
        url = f'{self.url}{path}'
        query = urllib.parse.urlencode(_encode_params(params))
        if query:
            url = f'{url}?{query}'
        data = None
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': '*/*',
            'User-Agent': f'scd-client/{__version__}',
        }
        if json_body is not None:
            data = json.dumps(json_body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if self.verbose:
            print(f'> {method} {url}', file=sys.stderr)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as r:
                resp = Response(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as exc:
            resp = Response(exc.code, dict(exc.headers or {}), exc.read() or b'')
        except urllib.error.URLError as exc:
            raise ScdNetworkError(f'Cannot reach {self.url}: {exc.reason}') from exc
        except (TimeoutError, OSError) as exc:
            raise ScdNetworkError(f'Request to {self.url} failed: {exc}') from exc
        if self.verbose:
            print(f'< {resp.status} {resp.content_type} {len(resp.body)} bytes', file=sys.stderr)
        if resp.status >= 400:
            raise self._error(resp)
        if 'text/html' in resp.content_type:
            # An SSO login page or proxy error, not the API.
            raise ScdApiError(resp.status, 'Server returned HTML instead of API output; '
                                           'check the URL (it must be the site root).')
        return resp

    @staticmethod
    def _error(resp):
        try:
            data = resp.json()
        except ValueError:
            return ScdApiError(resp.status, resp.body.decode('utf-8', 'replace')[:300].strip()
                               or 'empty response')
        return ScdApiError(resp.status, data.get('error') or 'Request rejected.',
                           data.get('errors'))

    # -- endpoints ------------------------------------------------------------

    def whoami(self):
        return self.request('GET', '/api/whoami/').json()

    def taxonomy(self, kind=None, include_inactive=False):
        path = f'/api/taxonomy/{kind}/' if kind else '/api/taxonomy/'
        return self.request('GET', path, {'all': include_inactive}).json()

    def entries(self, **filters):
        """One page of the caller's entries: {count, limit, offset, results}."""
        filters['format'] = 'json'
        return self.request('GET', '/api/entries/', filters).json()

    def iter_entries(self, page_size=500, **filters):
        offset = 0
        while True:
            page = self.entries(limit=page_size, offset=offset, **filters)
            yield from page['results']
            offset += len(page['results'])
            if not page['results'] or offset >= page['count']:
                return

    def export_entries(self, fmt, **filters):
        """All of the caller's matching entries in an export format -> Response."""
        filters['format'] = fmt
        return self.request('GET', '/api/entries/', filters)

    def entry(self, entry_id, fmt='json'):
        resp = self.request('GET', f'/api/entries/{int(entry_id)}/', {'format': fmt})
        return resp.json() if fmt == 'json' else resp

    def report(self, fmt='json', **filters):
        """Run a report. JSON is parsed; other formats return the Response."""
        body = dict(filters, format=fmt)
        resp = self.request('POST', '/api/reports/', json_body=body)
        return resp.json() if fmt == 'json' else resp

    def report_count(self, **filters):
        body = dict(filters, count=True)
        return self.request('POST', '/api/reports/', json_body=body).json()['count']

    def summary(self, fmt='json', template=None, **filters):
        body = dict(filters, format=fmt)
        if template:
            body['template'] = template
        resp = self.request('POST', '/api/reports/summary/', json_body=body)
        return resp.json() if fmt == 'json' else resp

    def create_entry(self, payload):
        return self.request('POST', '/api/entries/', json_body=payload).json()


# ── CLI helpers ───────────────────────────────────────────────────────────────

def _connection_parser():
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group('connection (override environment, .env and config file)')
    g.add_argument('-c', '--config', metavar='FILE',
                   help=f'YAML config file (default: $SCD_CONFIG or {DEFAULT_CONFIG_FILE})')
    g.add_argument('-u', '--url', metavar='URL', help='server root URL [SCD_URL]')
    g.add_argument('-t', '--token', metavar='TOKEN', help='API token [SCD_TOKEN]')
    g.add_argument('--token-file', metavar='FILE', help='read the token from FILE [SCD_TOKEN_FILE]')
    g.add_argument('--timeout', metavar='SEC', help='request timeout in seconds [SCD_TIMEOUT, 300]')
    g.add_argument('--ca-bundle', metavar='FILE', help='CA certificates for TLS [SCD_CA_BUNDLE]')
    g.add_argument('-k', '--insecure', action='store_true', default=None,
                   help='skip TLS certificate verification [SCD_INSECURE]')
    g.add_argument('-v', '--verbose', action='store_true', help='log requests to stderr')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return p


def _date_arg(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f'"{value}" is not a YYYY-MM-DD date')


def _add_period_args(g):
    g.add_argument('--from', dest='period_after', metavar='DATE', type=_date_arg,
                   help='range start, YYYY-MM-DD')
    g.add_argument('--to', dest='period_before', metavar='DATE', type=_date_arg,
                   help='range end, YYYY-MM-DD')
    g.add_argument('--period', choices=PERIODS,
                   help='named range; --from/--to override its ends')


def _report_filter_parser():
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group('report filters (slugs or numeric ids; repeat or comma-separate)')
    g.add_argument('-s', '--search', metavar='TEXT',
                   help='title/description search; supports AND, OR, XOR and "quoted phrases"')
    g.add_argument('-a', '--author', metavar='EMAIL', help='author email (substring match)')
    g.add_argument('-p', '--project', dest='projects', action='append', metavar='SLUG')
    g.add_argument('--category', dest='categories', action='append', metavar='SLUG')
    g.add_argument('--lab-priority', dest='lab_priorities', action='append', metavar='SLUG')
    g.add_argument('-g', '--group', metavar='SLUG', help='activity group of the entry')
    g.add_argument('--employee-group', metavar='SLUG', help="author's own group")
    g.add_argument('--entry-type', metavar='SLUG')
    _add_period_args(g)
    g.add_argument('--overlap', action='store_true',
                   help='match entries overlapping the range (default: entirely within it)')
    priv = g.add_mutually_exclusive_group()
    priv.add_argument('--private-only', action='store_true')
    priv.add_argument('--exclude-private', action='store_true')
    g.add_argument('--archived', action='store_true', help='archived entries instead of current ones')
    g.add_argument('--id', dest='ids', action='append', metavar='ID', help='restrict to entry ids')
    return p


def _apply_period(args):
    if getattr(args, 'period', None):
        start, end = period_range(args.period)
        args.period_after = args.period_after or start.isoformat()
        args.period_before = args.period_before or end.isoformat()


def report_filters(args):
    """argparse namespace -> /api/reports/ filter dict."""
    _apply_period(args)
    f = {
        'search': args.search,
        'author_email': args.author,
        'projects': args.projects,
        'categories': args.categories,
        'lab_priorities': args.lab_priorities,
        'group': args.group,
        'employee_group': args.employee_group,
        'entry_type': args.entry_type,
        'period_after': args.period_after,
        'period_before': args.period_before,
        'date_match': 'overlap' if args.overlap else None,
        'is_private': True if args.private_only else None,
        'exclude_private': True if args.exclude_private else None,
        'show_archived': True if args.archived else None,
        'selected_ids': args.ids,
    }
    return {k: v for k, v in f.items() if v not in (None, [], '')}


def _output_args(p, default_help):
    p.add_argument('-f', '--format', metavar='FMT', help=default_help)
    out = p.add_mutually_exclusive_group()
    out.add_argument('-o', '--output', metavar='FILE', help='write to FILE (default: stdout)')
    out.add_argument('-O', '--remote-name', action='store_true',
                     help='write to the file name suggested by the server')


def write_output(resp, args, fmt):
    if args.remote_name:
        path = resp.filename or f'scd_output.{fmt}'
    else:
        path = args.output
    if path and path != '-':
        Path(path).write_bytes(resp.body)
        print(f'Wrote {len(resp.body)} bytes to {path}', file=sys.stderr)
        return
    if fmt in BINARY_FORMATS and sys.stdout.isatty():
        raise ScdError(f'{fmt} is binary; use -o FILE or -O, or redirect stdout.')
    sys.stdout.buffer.write(resp.body)
    if resp.body and not resp.body.endswith(b'\n') and sys.stdout.isatty():
        sys.stdout.write('\n')
    sys.stdout.flush()


def _settings(args):
    return load_settings({
        'config': args.config, 'url': args.url, 'token': args.token,
        'token_file': args.token_file, 'timeout': args.timeout,
        'ca_bundle': args.ca_bundle, 'insecure': args.insecure,
    })


def _client(args):
    settings = _settings(args)
    return Client.from_settings(settings, verbose=args.verbose), settings


def _table(rows, columns):
    """Print rows (list of dicts) as an aligned plain-text table."""
    if not rows:
        print('(none)')
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ''))) for r in rows)) for c in columns}
    print('  '.join(c.upper().ljust(widths[c]) for c in columns).rstrip())
    for r in rows:
        print('  '.join(str(r.get(c, '')).ljust(widths[c]) for c in columns).rstrip())


def _print_json(data):
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write('\n')


# ── Tools ─────────────────────────────────────────────────────────────────────

def cmd_whoami(argv=None):
    p = argparse.ArgumentParser(prog='scd-whoami', parents=[_connection_parser()],
                                description='Show the user, role and report scope of your API token.')
    p.add_argument('--json', action='store_true', help='raw JSON')
    p.add_argument('--settings', action='store_true',
                   help='show resolved client settings and their sources, then exit (no request)')
    args = p.parse_args(argv)
    if args.settings:
        s = _settings(args)
        for k in SETTINGS:
            v = s[k]
            if k == 'token' and v:
                v = f'{v[:4]}…{v[-4:]}' if len(v) > 8 else '****'
            print(f'{k:<11} {v!s:<40} ({s["sources"][k]})')
        return EXIT_OK
    client, _ = _client(args)
    data = client.whoami()
    if args.json:
        _print_json(data)
        return EXIT_OK

    def scope(v):
        return 'all' if v is None else (', '.join(x['slug'] for x in v) or '(none)')
    print(f"User:          {data['display_name'] or data['username']} <{data['email']}>")
    print(f"Role:          {data['role_label']} ({data['role']})")
    print(f"Group:         {data['group']['name'] if data['group'] else '(none)'}")
    print(f"Can report:    {'yes' if data['can_report'] else 'no'}")
    if data['can_report']:
        print(f"Group scope:   {scope(data['report_group_scope'])}")
        print(f"Project scope: {scope(data['report_project_scope'])}")
    print(f"Formats:       {', '.join(data['export_formats'])}")
    print(f"Token used:    {data['token']['last_used_at'] or 'never'}")
    print(f"Server:        {client.url}")
    return EXIT_OK


def cmd_taxonomy(argv=None):
    p = argparse.ArgumentParser(prog='scd-taxonomy', parents=[_connection_parser()],
                                description='List taxonomy values (the slugs the filters accept).')
    p.add_argument('kind', nargs='?', choices=TAXONOMY_KINDS, help='one table (default: all)')
    p.add_argument('--all', action='store_true', help='include inactive values')
    p.add_argument('--json', action='store_true', help='raw JSON')
    args = p.parse_args(argv)
    client, _ = _client(args)
    data = client.taxonomy(args.kind, include_inactive=args.all)
    if args.json:
        _print_json(data)
        return EXIT_OK
    for kind, rows in data.items():
        if len(data) > 1:
            print(f'\n== {kind} ==')
        if kind == 'tags':
            _table(rows, ['id', 'name', 'use_count'])
        else:
            for r in rows:
                r['active'] = 'yes' if r['is_active'] else 'no'
            _table(rows, ['id', 'slug', 'name', 'short_code'] + (['active'] if args.all else []))
    return EXIT_OK


def cmd_entries(argv=None):
    p = argparse.ArgumentParser(prog='scd-entries', parents=[_connection_parser()],
                                description='List or export your own entries (the My Entries scope).')
    g = p.add_argument_group('filters')
    g.add_argument('-q', '--query', metavar='TEXT', help='substring of title, description or tag')
    g.add_argument('-p', '--project', dest='projects', action='append', metavar='SLUG')
    g.add_argument('--category', dest='categories', action='append', metavar='SLUG')
    _add_period_args(g)
    g.add_argument('--archived', choices=('0', '1', 'all'), default='0',
                   help='0 current (default), 1 archived only, all both')
    p.add_argument('-n', '--limit', type=int, default=50, help='rows to show (default 50)')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--all-pages', action='store_true', help='fetch every matching entry')
    p.add_argument('--json', action='store_true', help='JSON entry objects instead of a table')
    _output_args(p, 'export every match as txt, md, csv, json, xlsx or pdf '
                    '(default: a table; ignores --limit)')
    args = p.parse_args(argv)
    _apply_period(args)
    filters = {'q': args.query, 'projects': args.projects, 'categories': args.categories,
               'period_after': args.period_after, 'period_before': args.period_before,
               'archived': args.archived}
    client, _ = _client(args)
    if args.format:
        write_output(client.export_entries(args.format, **filters), args, args.format)
        return EXIT_OK
    if args.all_pages:
        results = list(client.iter_entries(**filters))
        total = len(results)
    else:
        page = client.entries(limit=args.limit, offset=args.offset, **filters)
        results, total = page['results'], page['count']
    if args.json:
        _print_json(results)
        return EXIT_OK
    rows = [{
        'id': e['id'],
        'period': f"{e['period_start']}..{e['period_end']}",
        'projects': ','.join(x['slug'] for x in e['projects']),
        'flags': ''.join(c for c, on in (('P', e['is_private']), ('C', e['is_critical']),
                                         ('H', e['is_highlight']), ('A', e['is_archived'])) if on),
        'title': e['title'] if len(e['title']) <= 70 else e['title'][:69] + '…',
    } for e in results]
    _table(rows, ['id', 'period', 'projects', 'flags', 'title'])
    if len(results) < total:
        print(f'\n{len(results)} of {total} shown (use --all-pages, --limit or --offset)',
              file=sys.stderr)
    return EXIT_OK


def cmd_entry(argv=None):
    p = argparse.ArgumentParser(prog='scd-entry', parents=[_connection_parser()],
                                description='Show one entry by id.')
    p.add_argument('id', type=int)
    _output_args(p, 'md (default), txt, json, csv, xlsx or pdf')
    args = p.parse_args(argv)
    fmt = (args.format or 'md').lower()
    client, _ = _client(args)
    if fmt == 'json':
        data = client.entry(args.id, 'json')
        resp = Response(200, {}, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
    else:
        resp = client.entry(args.id, fmt)
    write_output(resp, args, fmt)
    return EXIT_OK


def cmd_report(argv=None):
    p = argparse.ArgumentParser(
        prog='scd-report', parents=[_connection_parser(), _report_filter_parser()],
        description='Run a report (same filters and scope as the Reports page) and download it.')
    _output_args(p, 'txt, md, csv, json, xlsx or pdf [SCD_FORMAT, default md]')
    p.add_argument('--count', action='store_true', help='print only the number of matching entries')
    args = p.parse_args(argv)
    filters = report_filters(args)
    client, settings = _client(args)
    if args.count:
        print(client.report_count(**filters))
        return EXIT_OK
    fmt = (args.format or settings['format']).lower()
    resp = client.request('POST', '/api/reports/', json_body=dict(filters, format=fmt))
    write_output(resp, args, fmt)
    return EXIT_OK


def cmd_summary(argv=None):
    p = argparse.ArgumentParser(
        prog='scd-summary', parents=[_connection_parser(), _report_filter_parser()],
        description='Generate the AI summary of a report selection (a paid model call).')
    _output_args(p, 'md (default), txt or json')
    p.add_argument('-T', '--template', metavar='NAME',
                   help='one of your saved prompt templates (name or id)')
    args = p.parse_args(argv)
    fmt = (args.format or 'md').lower()
    filters = report_filters(args)
    client, _ = _client(args)
    if fmt == 'json':
        data = client.summary('json', template=args.template, **filters)
        resp = Response(200, {}, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
        if data.get('truncated'):
            print('warning: summary was truncated at max_tokens', file=sys.stderr)
    else:
        resp = client.summary(fmt, template=args.template, **filters)
    write_output(resp, args, fmt)
    return EXIT_OK


COMMANDS = {
    'whoami': cmd_whoami,
    'taxonomy': cmd_taxonomy,
    'entries': cmd_entries,
    'entry': cmd_entry,
    'report': cmd_report,
    'summary': cmd_summary,
}


def run(command, argv=None):
    """Run a tool and map exceptions to exit codes. Returns the exit status."""
    try:
        return COMMANDS[command](argv)
    except ScdApiError as exc:
        print(f'error: {exc}', file=sys.stderr)
        if exc.status == 401:
            print('  The token is missing, wrong or revoked; rotate it at Profile > API Token.',
                  file=sys.stderr)
        return EXIT_API
    except ScdNetworkError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_NETWORK
    except ScdError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:   # e.g. piped into head
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help') or argv[0] not in COMMANDS:
        print(f'usage: scd_client.py {{{",".join(COMMANDS)}}} [options]\n'
              f'Run "scd_client.py COMMAND -h" for the options of each command.',
              file=sys.stderr)
        return EXIT_OK if argv and argv[0] in ('-h', '--help') else EXIT_USAGE
    return run(argv[0], argv[1:])


if __name__ == '__main__':
    sys.exit(main())
