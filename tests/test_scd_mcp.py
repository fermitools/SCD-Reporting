"""Tests for scripts/scd_mcp.py: JSON-RPC/MCP protocol handling, tool argument
handling, end-to-end stdio sessions against a live server, conformance with
the official MCP SDK client, and the /mcp/ instructions page."""
import io
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from django.urls import reverse

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
CLIENT_PY = os.environ.get('SCD_CLIENT_PYTHON', sys.executable)

import scd_mcp  # noqa: E402
from scd_mcp import McpServer, ToolRunner  # noqa: E402


# ── Protocol unit tests (no network) ──────────────────────────────────────────

def _server(tmp_path, env=None, cli=None):
    env = env if env is not None else {'SCD_URL': 'http://127.0.0.1:9', 'SCD_TOKEN': 't'}
    runner = ToolRunner(cli or {}, environ=env, cwd=tmp_path)
    runner._cwd = tmp_path
    return McpServer(runner, log=lambda m: None)


def _req(mid, method, params=None):
    msg = {'jsonrpc': '2.0', 'id': mid, 'method': method}
    if params is not None:
        msg['params'] = params
    return msg


def _init(srv, version='2025-11-25'):
    return srv.handle(_req(1, 'initialize', {'protocolVersion': version, 'capabilities': {},
                                             'clientInfo': {'name': 'test', 'version': '0'}}))


class TestProtocol:
    @pytest.mark.parametrize('requested,expected', [
        ('2025-11-25', '2025-11-25'), ('2025-06-18', '2025-06-18'),
        ('2024-11-05', '2024-11-05'), ('2026-07-28', '2025-11-25'), ('1999-01-01', '2025-11-25'),
    ])
    def test_version_negotiation(self, tmp_path, requested, expected):
        r = _init(_server(tmp_path), requested)['result']
        assert r['protocolVersion'] == expected
        assert r['capabilities'] == {'tools': {'listChanged': False}}
        assert r['serverInfo']['name'] == 'scd-reporting'
        assert 'scd_list_taxonomy' in r['instructions']

    def test_modern_discover_probe_gets_method_not_found(self, tmp_path):
        # The SDK's auto mode probes server/discover and falls back to initialize on -32601.
        r = _server(tmp_path).handle(_req(1, 'server/discover', {}))
        assert r['error']['code'] == -32601

    def test_ping_and_notifications(self, tmp_path):
        srv = _server(tmp_path)
        assert srv.handle(_req(7, 'ping'))['result'] == {}
        assert srv.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
        assert srv.handle({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                           'params': {'requestId': 3}}) is None
        # A notification for an unknown method still gets no reply.
        assert srv.handle({'jsonrpc': '2.0', 'method': 'whatever'}) is None

    def test_invalid_request(self, tmp_path):
        r = _server(tmp_path).handle({'id': 5, 'method': 'ping'})
        assert r['error']['code'] == -32600 and r['id'] == 5

    def test_tools_list_full(self, tmp_path):
        srv = _server(tmp_path)
        _init(srv)
        tools = srv.handle(_req(2, 'tools/list'))['result']['tools']
        assert [t['name'] for t in tools] == list(scd_mcp.TOOLS)
        for t in tools:
            assert t['inputSchema']['type'] == 'object'
            assert t['title'] and t['annotations']['title']
        by_name = {t['name']: t for t in tools}
        assert by_name['scd_create_entry']['annotations']['readOnlyHint'] is False
        assert by_name['scd_run_report']['annotations']['readOnlyHint'] is True

    def test_old_protocol_omits_newer_fields(self, tmp_path):
        srv = _server(tmp_path)
        _init(srv, '2024-11-05')
        tool = srv.handle(_req(2, 'tools/list'))['result']['tools'][0]
        assert 'annotations' not in tool and 'title' not in tool

    def test_read_only_and_no_summary(self, tmp_path):
        env = {'SCD_URL': 'http://x', 'SCD_TOKEN': 't', 'SCD_MCP_READ_ONLY': '1'}
        srv = _server(tmp_path, env=env, cli={'mcp_no_summary': True})
        names = [t['name'] for t in srv.handle(_req(2, 'tools/list'))['result']['tools']]
        assert 'scd_create_entry' not in names and 'scd_report_summary' not in names
        r = srv.handle(_req(3, 'tools/call', {'name': 'scd_create_entry', 'arguments': {}}))
        assert r['error']['code'] == -32602

    def test_unknown_tool_is_protocol_error(self, tmp_path):
        r = _server(tmp_path).handle(_req(3, 'tools/call', {'name': 'nope', 'arguments': {}}))
        assert r['error']['code'] == -32602

    @pytest.mark.parametrize('name,args,needle', [
        ('scd_whoami', {'bogus': 1}, 'Unknown argument'),
        ('scd_get_entry', {}, 'Missing required argument(s): id'),
        ('scd_create_entry', {'title': 't', 'description': 'd', 'projects': ['p'],
                              'categories': ['c']}, 'Give period'),
        ('scd_run_report', {'period_after': '2026-13-40'}, 'YYYY-MM-DD'),
    ])
    def test_tool_argument_errors(self, tmp_path, name, args, needle):
        r = _server(tmp_path).handle(_req(3, 'tools/call', {'name': name, 'arguments': args}))
        res = r['result']
        assert res['isError'] is True
        assert needle in res['content'][0]['text']

    def test_missing_settings_is_tool_error_not_crash(self, tmp_path):
        srv = _server(tmp_path, env={})
        r = srv.handle(_req(3, 'tools/call', {'name': 'scd_whoami', 'arguments': {}}))
        assert r['result']['isError'] is True
        assert 'No server URL' in r['result']['content'][0]['text']

    def test_unreachable_server_is_tool_error(self, tmp_path):
        r = _server(tmp_path).handle(_req(3, 'tools/call', {'name': 'scd_whoami', 'arguments': {}}))
        assert r['result']['isError'] is True
        assert 'Cannot reach' in r['result']['content'][0]['text']

    def test_serve_loop_parse_error_and_batch(self, tmp_path):
        srv = _server(tmp_path)
        lines = [b'not json',
                 json.dumps([_req(1, 'ping'), {'jsonrpc': '2.0', 'method': 'notifications/x'}]).encode(),
                 b'',
                 json.dumps(_req(2, 'ping')).encode()]
        out = io.BytesIO()
        srv.serve(stdin=io.BytesIO(b'\n'.join(lines) + b'\n'), stdout=out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        assert replies[0]['error']['code'] == -32700
        assert replies[1] == [{'jsonrpc': '2.0', 'id': 1, 'result': {}}]
        assert replies[2]['id'] == 2


def test_create_period_mapping(tmp_path, monkeypatch):
    srv = _server(tmp_path)
    captured = {}
    monkeypatch.setattr(srv.runner, 'client', lambda: type('C', (), {
        'create_entry': staticmethod(lambda p: captured.update(p) or {'id': 1})})())
    srv.runner.call('scd_create_entry', {'title': 't', 'description': 'd', 'projects': 'dune,nova',
                                         'categories': ['c'], 'period': 'last-month'})
    assert captured['period_kind'] == 'month'
    assert captured['projects'] == ['dune', 'nova']
    assert captured['period_start'].endswith('-01')


# ── End to end over stdio against a live server ───────────────────────────────

@pytest.fixture
def live(live_server, transactional_db, tmp_path):
    from apps.accounts.models import APIToken, User
    from apps.entries.models import WorkItem
    from apps.taxonomy.models import Category, Project

    dune = Project.objects.create(name='DUNE', slug='dune')
    Project.objects.create(name='NOvA', slug='nova')
    sci = Category.objects.create(name='Scientific', slug='scientific')
    auditor = User.objects.create_user(username='aud', email='aud@example.com', password='x',
                                       role=User.Role.AUDITOR)
    user = User.objects.create_user(username='u', email='u@example.com', password='x')
    e = WorkItem.objects.create(author=user, title='Existing DUNE work', description='old',
                                period_kind='week', period_start=date(2026, 10, 5),
                                period_end=date(2026, 10, 11))
    e.projects.set([dune])
    e.categories.set([sci])
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('SCD_')}
    env.update({'HOME': str(home), 'USERPROFILE': str(home), 'SCD_URL': live_server.url})
    return {'env': env, 'cwd': tmp_path, 'auditor': APIToken.rotate(auditor).key,
            'user': APIToken.rotate(user).key}


class StdioSession:
    def __init__(self, live, token, *extra):
        env = dict(live['env'], SCD_TOKEN=live[token])
        self.proc = subprocess.Popen([CLIENT_PY, str(SCRIPTS / 'scd-mcp'), *extra], env=env,
                                     cwd=live['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.next_id = 0
        init = self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                           'clientInfo': {'name': 'pytest', 'version': '0'}})
        assert init['result']['protocolVersion'] == '2025-06-18'
        self.notify('notifications/initialized')

    def notify(self, method):
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method}).encode() + b'\n')
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        msg = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}}
        self.proc.stdin.write(json.dumps(msg).encode() + b'\n')
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply['id'] == self.next_id
        return reply

    def call(self, name, **args):
        return self.request('tools/call', {'name': name, 'arguments': args})['result']

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)
        err = self.proc.stderr.read().decode()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return err


@pytest.fixture
def session(live):
    sessions = []

    def open_(token='user', *extra):
        s = StdioSession(live, token, *extra)
        sessions.append(s)
        return s
    yield open_
    for s in sessions:
        s.close()


class TestEndToEnd:
    def test_post_then_retrieve(self, session, live):
        s = session('user')
        created = s.call('scd_create_entry', title='MCP posted entry', description='Posted via **MCP**.',
                         projects=['dune'], categories=['scientific'], period='this-week',
                         tags=['mcp'])
        assert created['isError'] is False, created
        new_id = created['structuredContent']['id']

        page = s.call('scd_list_my_entries', q='MCP posted')['structuredContent']
        assert page['count'] == 1 and page['results'][0]['id'] == new_id
        assert page['results'][0]['period_kind'] == 'week'
        assert page['results'][0]['tags'] == ['mcp']

        md = s.call('scd_get_entry', id=new_id, format='md')
        assert md['content'][0]['text'].startswith('## MCP posted entry')

        export = s.call('scd_export_my_entries', format='md', projects=['dune'])
        assert 'Existing DUNE work' in export['content'][0]['text']

    def test_reports_as_auditor(self, session, live):
        s = session('auditor')
        who = s.call('scd_whoami')['structuredContent']
        assert who['can_report'] is True and who['server'] == live['env']['SCD_URL']
        report = s.call('scd_run_report', projects=['dune'], format='md')
        assert report['isError'] is False
        assert '## Existing DUNE work' in report['content'][0]['text']
        assert s.call('scd_count_report', search='Existing')['structuredContent'] == {'count': 1}
        rows = json.loads(s.call('scd_run_report', format='json')['content'][0]['text'])
        assert [r['title'] for r in rows] == ['Existing DUNE work']
        tax = s.call('scd_list_taxonomy', kind='projects')['structuredContent']
        assert {p['slug'] for p in tax['projects']} == {'dune', 'nova'}

    def test_binary_report_to_file_and_overwrite_guard(self, session, live):
        s = session('auditor')
        out = live['cwd'] / 'report.xlsx'
        r = s.call('scd_run_report', format='xlsx', output_path=str(out))
        assert r['isError'] is False and out.read_bytes()[:2] == b'PK'
        assert r['structuredContent']['path'] == str(out)
        again = s.call('scd_run_report', format='xlsx', output_path=str(out))
        assert again['isError'] is True and 'overwrite' in again['content'][0]['text']
        inline = s.call('scd_run_report', format='pdf')
        assert inline['isError'] is True and 'output_path' in inline['content'][0]['text']

    def test_role_and_validation_errors_reach_the_model(self, session, live):
        s = session('user')
        r = s.call('scd_run_report', format='md')
        assert r['isError'] is True and 'HTTP 403' in r['content'][0]['text']
        r = s.call('scd_list_my_entries', projects=['atlas'])
        assert r['isError'] is True and 'atlas' in r['content'][0]['text']
        bad = s.call('scd_create_entry', title='x', description='y', projects=['dune'],
                     categories=['nope'], period='today')
        assert bad['isError'] is True and 'categories' in bad['content'][0]['text']

    def test_summary_error_when_model_not_configured(self, session, live, settings):
        settings.ANTHROPIC_API_KEY = ''
        r = session('auditor').call('scd_report_summary', projects=['dune'])
        assert r['isError'] is True and 'HTTP 400' in r['content'][0]['text']

    def test_read_only_flag(self, session, live):
        s = session('user', '--read-only')
        names = [t['name'] for t in s.request('tools/list')['result']['tools']]
        assert 'scd_create_entry' not in names and 'scd_list_my_entries' in names

    def test_bad_token(self, session, live):
        live['bad'] = 'wrong'
        r = session('bad').call('scd_whoami')
        assert r['isError'] is True and 'HTTP 401' in r['content'][0]['text']


# ── Conformance with the official MCP SDK client ──────────────────────────────

class TestSdkConformance:
    def test_sdk_client_auto_mode(self, live):
        pytest.importorskip('mcp')
        anyio = pytest.importorskip('anyio')
        from mcp.client.client import Client
        from mcp.client.stdio import StdioServerParameters

        params = StdioServerParameters(command=CLIENT_PY, args=[str(SCRIPTS / 'scd-mcp')],
                                       env=dict(live['env'], SCD_TOKEN=live['auditor']),
                                       cwd=str(live['cwd']))
        out = {}

        async def run():
            # mode='auto' probes server/discover (2026-07-28) and must fall back to initialize.
            async with Client(params) as c:
                out['version'] = c.protocol_version
                out['tools'] = [t.name for t in (await c.list_tools()).tools]
                res = await c.call_tool('scd_run_report', {'format': 'md', 'projects': ['dune']})
                out['report'] = res
                out['who'] = await c.call_tool('scd_whoami', {})

        anyio.run(run)
        assert out['version'] == '2025-11-25'
        assert out['tools'] == list(scd_mcp.TOOLS)
        assert not getattr(out['report'], 'is_error', getattr(out['report'], 'isError', False))
        assert '## Existing DUNE work' in out['report'].content[0].text
        structured = getattr(out['who'], 'structured_content', None) or getattr(out['who'], 'structuredContent')
        assert structured['role'] == 'auditor'


# ── /mcp/ instructions page ───────────────────────────────────────────────────

class TestMcpPage:
    @pytest.fixture
    def user(self, db):
        from apps.accounts.models import User
        return User.objects.create_user(username='p', email='p@example.com', password='x')

    def test_requires_login(self, client, db):
        assert client.get(reverse('mcp')).status_code == 302

    def test_page_shows_connection_details(self, client, user):
        from apps.accounts.models import APIToken
        client.force_login(user)
        body = client.get(reverse('mcp')).content.decode()
        assert 'http://testserver' in body                       # detected server URL
        assert 'claude mcp add --scope user scd-reporting' in body
        assert 'generate one on your Profile' in body            # no token yet
        token = APIToken.rotate(user)
        body = client.get(reverse('mcp')).content.decode()
        assert 'Active' in body
        assert token.key not in body                            # never print the token

    def test_page_tool_list_matches_server(self, client, user):
        from apps.core.mcp_doc import MCP_TOOLS
        assert [t[0] for t in MCP_TOOLS] == list(scd_mcp.TOOLS)
        assert {t[0] for t in MCP_TOOLS if t[1] == 'write'} == {
            n for n, t in scd_mcp.TOOLS.items() if t.get('flag') == 'write'}
        client.force_login(user)
        body = client.get(reverse('mcp')).content.decode()
        for name in scd_mcp.TOOLS:
            assert name in body

    def test_footer_links_to_page(self, client, user):
        client.force_login(user)
        assert reverse('mcp') in client.get(reverse('api')).content.decode()

    def test_project_mcp_json_points_at_server(self):
        cfg = json.loads((SCRIPTS.parent / '.mcp.json').read_text())
        srv = cfg['mcpServers']['scd-reporting']
        assert srv['args'] == ['scripts/scd-mcp']
        assert (SCRIPTS.parent / srv['args'][0]).is_file()
