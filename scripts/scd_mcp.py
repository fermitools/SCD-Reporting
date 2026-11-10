#!/usr/bin/env python3
"""scd_mcp — Model Context Protocol server for SCD Reporting (stdio transport).

Exposes every token-API route as an MCP tool so that an MCP client (Claude Code,
Claude Desktop, ...) can post and retrieve entries, run reports and generate
summaries. HTTP and configuration are handled by scd_client.py, so the server
uses exactly the same settings as the scd-* command-line tools:

    command line > environment > .env > ~/.config/scd-reporting/config.yaml > default

Implemented directly on JSON-RPC 2.0 over stdin/stdout with the Python standard
library (the official MCP SDK needs Python >= 3.10; this keeps the 3.9 floor).
Protocol versions: the initialize-handshake family 2024-11-05 .. 2025-11-25.

Register with Claude Code:

    claude mcp add scd-reporting -- python3 /path/to/SCD-Reporting/scripts/scd-mcp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import scd_client  # noqa: E402
from scd_client import (  # noqa: E402
    PERIODS, TAXONOMY_KINDS, Client, ScdApiError, ScdError, ScdNetworkError,
    load_settings, period_range,
)

SERVER_NAME = 'scd-reporting'
SERVER_VERSION = scd_client.__version__
PROTOCOL_VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
LATEST_PROTOCOL = PROTOCOL_VERSIONS[-1]
MAX_INLINE_CHARS = 400_000

EXPORT_FORMATS = ('txt', 'md', 'csv', 'json', 'xlsx', 'pdf')
BINARY_FORMATS = scd_client.BINARY_FORMATS

INSTRUCTIONS = """\
SCD Reporting records the work of Fermilab Scientific Computing Division staff as
entries (title, Markdown description, projects, categories, period, flags).

- Filters take taxonomy slugs; call scd_list_taxonomy first if you do not know them.
- scd_list_my_entries / scd_export_my_entries see only the caller's own entries.
- scd_run_report, scd_count_report and scd_report_summary cover everyone in the
  caller's report scope and need a reporting role (see scd_whoami.can_report).
- Report dates default to entries entirely inside [period_after, period_before];
  pass date_match="overlap" for entries that merely touch the range.
- scd_create_entry posts a real entry under the caller's name.
- scd_report_summary is a paid model call; prefer scd_run_report(format="md")
  when the raw entries are enough.
- xlsx and pdf are binary: pass output_path to save them to a file.
"""

# ── JSON Schema building blocks ───────────────────────────────────────────────

def _strs(desc):
    return {'type': 'array', 'items': {'type': 'string'},
            'description': desc + ' Slugs or numeric ids (see scd_list_taxonomy).'}


DATE = {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'}
PERIOD = {'type': 'string', 'enum': list(PERIODS),
          'description': 'Named range relative to today (weeks start Monday, calendar quarters). '
                         'Explicit period_after/period_before override either end.'}

REPORT_FILTERS = {
    'search': {'type': 'string', 'description': 'Title/description search. Words are ORed; '
               'AND, OR, XOR and "quoted phrases" are supported, left to right.'},
    'author_email': {'type': 'string', 'description': 'Author email, case-insensitive substring.'},
    'projects': _strs('Projects (match any).'),
    'categories': _strs('Categories (match any).'),
    'lab_priorities': _strs('Lab priorities (match any).'),
    'group': {'type': 'string', 'description': 'Activity group recorded on the entry (slug or id).'},
    'employee_group': {'type': 'string', 'description': "The author's own group (slug or id)."},
    'entry_type': {'type': 'string', 'description': 'Entry type (slug or id).'},
    'period': PERIOD,
    'period_after': dict(DATE, description='Range start, YYYY-MM-DD.'),
    'period_before': dict(DATE, description='Range end, YYYY-MM-DD.'),
    'date_match': {'type': 'string', 'enum': ['contained', 'overlap'],
                   'description': 'contained (default): entry period entirely inside the range; '
                                  'overlap: entry period touches the range.'},
    'is_private': {'type': 'boolean', 'description': 'Only private entries.'},
    'exclude_private': {'type': 'boolean', 'description': 'Exclude private entries.'},
    'show_archived': {'type': 'boolean', 'description': 'Archived entries instead of current ones.'},
    'selected_ids': {'type': 'array', 'items': {'type': 'integer'},
                     'description': 'Restrict to these entry ids.'},
}

ENTRY_FILTERS = {
    'q': {'type': 'string', 'description': 'Substring of title, description or a tag name.'},
    'projects': _strs('Projects (match any).'),
    'categories': _strs('Categories (match any).'),
    'period': PERIOD,
    'period_after': dict(DATE, description='Entries whose period ends on or after this date.'),
    'period_before': dict(DATE, description='Entries whose period starts on or before this date.'),
    'archived': {'type': 'string', 'enum': ['0', '1', 'all'],
                 'description': '0 current entries (default), 1 archived only, all both.'},
}

OUTPUT = {
    'output_path': {'type': 'string', 'description': 'Save the result to this file instead of returning '
                    'it (required for xlsx and pdf). ~ is expanded; relative paths are relative '
                    'to the server working directory.'},
    'overwrite': {'type': 'boolean', 'description': 'Replace output_path if it exists (default false).'},
}


def _obj(props, required=()):
    schema = {'type': 'object', 'properties': props, 'additionalProperties': False}
    if required:
        schema['required'] = list(required)
    return schema


READ = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}

# name -> definition. 'handler' names a method of ToolRunner; 'flag' gates registration.
TOOLS = {
    'scd_whoami': {
        'title': 'Who am I',
        'description': 'The account behind the API token: role, group, whether it can run reports, '
                       'its report group/project scope (null = unrestricted) and export formats. '
                       'Use it to check the connection.',
        'inputSchema': _obj({}),
        'annotations': READ,
    },
    'scd_list_taxonomy': {
        'title': 'List taxonomy',
        'description': 'Projects, categories, entry types, groups, lab priorities and tags with the '
                       'slugs and ids that the filters and scd_create_entry accept.',
        'inputSchema': _obj({
            'kind': {'type': 'string', 'enum': list(TAXONOMY_KINDS), 'description': 'One table; default all.'},
            'include_inactive': {'type': 'boolean', 'description': 'Include inactive values.'},
        }),
        'annotations': READ,
    },
    'scd_list_my_entries': {
        'title': 'List my entries',
        'description': "The caller's own entries (the My Entries page), newest period first, as "
                       'structured objects with paging: {count, limit, offset, results}.',
        'inputSchema': _obj(dict(ENTRY_FILTERS, **{
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'description': 'Page size (default 25).'},
            'offset': {'type': 'integer', 'minimum': 0, 'description': 'Skip this many matches.'},
        })),
        'annotations': READ,
    },
    'scd_get_entry': {
        'title': 'Get entry',
        'description': 'One entry by id, visible under the same rules as the web entry page. '
                       'format json (default) gives the structured object; md is a readable rendering.',
        'inputSchema': _obj(dict({
            'id': {'type': 'integer', 'minimum': 1},
            'format': {'type': 'string', 'enum': list(EXPORT_FORMATS), 'description': 'Default json.'},
        }, **OUTPUT), required=['id']),
        'annotations': READ,
    },
    'scd_export_my_entries': {
        'title': 'Export my entries',
        'description': "All of the caller's matching entries in an export format (no paging). "
                       'Recorded in the audit log.',
        'inputSchema': _obj(dict(ENTRY_FILTERS, format={'type': 'string', 'enum': list(EXPORT_FORMATS),
                                                        'description': 'Default md.'}, **OUTPUT)),
        'annotations': READ,
    },
    'scd_create_entry': {
        'title': 'Create entry',
        'description': 'Post a new work entry under the token owner\'s name. Give either period '
                       '(sets dates and period_kind) or period_start and period_end. Returns the '
                       'new entry id and URL.',
        'inputSchema': _obj({
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'description': {'type': 'string', 'minLength': 1, 'description': 'Markdown.'},
            'projects': _strs('One or more projects.'),
            'categories': _strs('One or more categories.'),
            'period': {'type': 'string', 'enum': ['today', 'this-week', 'last-week', 'this-month',
                                                  'last-month', 'this-quarter', 'last-quarter',
                                                  'this-year', 'last-year']},
            'period_start': DATE,
            'period_end': DATE,
            'period_kind': {'type': 'string', 'enum': ['today', 'week', 'fortnight', 'month', 'custom'],
                            'description': 'Default derived from period, else week.'},
            'group': {'type': 'string', 'description': 'Activity group slug or id (optional).'},
            'lab_priorities': _strs('Lab priorities (optional).'),
            'tags': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Free-form tags.'},
            'is_private': {'type': 'boolean'},
            'is_critical': {'type': 'boolean'},
            'is_highlight': {'type': 'boolean'},
            'highlight_stars': {'type': 'integer', 'minimum': 0, 'maximum': 5},
            'is_division_head_only': {'type': 'boolean'},
        }, required=['title', 'description', 'projects', 'categories']),
        'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                        'idempotentHint': False, 'openWorldHint': True},
        'flag': 'write',
    },
    'scd_run_report': {
        'title': 'Run report',
        'description': 'Run a Reports-page report over everyone in the caller\'s report scope and '
                       'return it (md default; json gives flat rows; txt, csv; xlsx/pdf need '
                       'output_path). Needs a reporting role. Recorded in the audit log.',
        'inputSchema': _obj(dict(REPORT_FILTERS, format={'type': 'string', 'enum': list(EXPORT_FORMATS),
                                                         'description': 'Default md.'}, **OUTPUT)),
        'annotations': READ,
    },
    'scd_count_report': {
        'title': 'Count report',
        'description': 'Number of entries a report with these filters would contain. Cheap; not audited.',
        'inputSchema': _obj(REPORT_FILTERS),
        'annotations': READ,
    },
    'scd_report_summary': {
        'title': 'AI report summary',
        'description': 'AI-written summary of the entries a report would contain, using the '
                       'caller\'s saved prompt configuration or a named template. A paid model '
                       'call that can take minutes; needs a reporting role.',
        'inputSchema': _obj(dict(REPORT_FILTERS, **{
            'template': {'type': 'string', 'description': 'Saved prompt template name or id.'},
            'format': {'type': 'string', 'enum': ['md', 'txt', 'json'],
                       'description': 'md (default); json adds token counts and truncation flag.'},
        }, **OUTPUT)),
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': False, 'openWorldHint': True},
        'flag': 'summary',
    },
}

CREATE_PERIOD_KIND = {'today': 'today', 'this-week': 'week', 'last-week': 'week',
                      'this-month': 'month', 'last-month': 'month'}


class ToolError(Exception):
    """A tool-level failure reported to the model as ``isError: true``."""


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ── Tool implementations ──────────────────────────────────────────────────────

def _list(v):
    if v is None:
        return None
    if isinstance(v, str):
        return [s.strip() for s in v.split(',') if s.strip()]
    return list(v)


def _apply_period(args):
    p = args.pop('period', None)
    if p:
        start, end = period_range(p)
        args.setdefault('period_after', start.isoformat())
        args.setdefault('period_before', end.isoformat())
    return args


def _check_date(args, *keys):
    for k in keys:
        if args.get(k):
            try:
                date.fromisoformat(args[k])
            except (TypeError, ValueError):
                raise ToolError(f'{k} must be a YYYY-MM-DD date, got {args[k]!r}.')


class ToolRunner:
    def __init__(self, settings_cli, environ=None, cwd=None):
        self._cli = settings_cli
        self._environ = environ
        self._cwd = cwd
        self._client = None
        self.settings = None
        self.settings_error = None
        try:
            self.settings = load_settings(settings_cli, environ=environ, cwd=cwd)
        except ScdError as exc:
            self.settings_error = str(exc)

    @property
    def read_only(self):
        return bool(self.settings) and scd_client._truthy(self.settings['mcp_read_only'])

    @property
    def no_summary(self):
        return bool(self.settings) and scd_client._truthy(self.settings['mcp_no_summary'])

    def enabled_tools(self):
        out = []
        for name, t in TOOLS.items():
            if t.get('flag') == 'write' and self.read_only:
                continue
            if t.get('flag') == 'summary' and self.no_summary:
                continue
            out.append(name)
        return out

    def client(self):
        if self._client is None:
            if self.settings_error:
                raise ToolError(f'Configuration error: {self.settings_error}')
            try:
                self._client = Client.from_settings(self.settings)
            except ScdError as exc:
                raise ToolError(f'{exc} The server reads the same settings as the scd-* tools '
                                '(see `scd-whoami --settings`).')
        return self._client

    # -- output ---------------------------------------------------------------

    def _deliver(self, resp, fmt, args):
        path = args.get('output_path')
        if path:
            p = Path(os.path.expanduser(path))
            if not p.is_absolute():
                p = Path(self._cwd or os.getcwd()) / p
            if p.exists() and not args.get('overwrite'):
                raise ToolError(f'{p} exists; pass overwrite=true to replace it.')
            if not p.parent.is_dir():
                raise ToolError(f'Directory {p.parent} does not exist.')
            p.write_bytes(resp.body)
            return {'text': f'Wrote {len(resp.body)} bytes ({fmt}) to {p}',
                    'structured': {'path': str(p), 'bytes': len(resp.body), 'format': fmt}}
        if fmt in BINARY_FORMATS:
            raise ToolError(f'{fmt} is binary; pass output_path to save it to a file.')
        text = resp.text()
        if len(text) > MAX_INLINE_CHARS:
            raise ToolError(f'Result is {len(text)} characters; narrow the filters or pass output_path.')
        return {'text': text or '(empty)'}

    # -- filters --------------------------------------------------------------

    @staticmethod
    def _report_filters(args):
        f = {k: args[k] for k in REPORT_FILTERS if k in args and args[k] not in (None, '', [])}
        for k in ('projects', 'categories', 'lab_priorities'):
            if k in f:
                f[k] = _list(f[k])
        _apply_period(f)
        _check_date(f, 'period_after', 'period_before')
        return f

    @staticmethod
    def _entry_filters(args):
        f = {k: args[k] for k in ENTRY_FILTERS if k in args and args[k] not in (None, '', [])}
        for k in ('projects', 'categories'):
            if k in f:
                f[k] = _list(f[k])
        _apply_period(f)
        _check_date(f, 'period_after', 'period_before')
        return f

    # -- tools ----------------------------------------------------------------

    def scd_whoami(self, args):
        data = self.client().whoami()
        data['server'] = self.client().url
        return {'json': data}

    def scd_list_taxonomy(self, args):
        return {'json': self.client().taxonomy(args.get('kind'), args.get('include_inactive', False))}

    def scd_list_my_entries(self, args):
        f = self._entry_filters(args)
        f['limit'] = args.get('limit', 25)
        f['offset'] = args.get('offset', 0)
        return {'json': self.client().entries(**f)}

    def scd_get_entry(self, args):
        fmt = args.get('format', 'json')
        if fmt == 'json' and not args.get('output_path'):
            return {'json': self.client().entry(args['id'], 'json')}
        resp = self.client().request('GET', f"/api/entries/{int(args['id'])}/", {'format': fmt})
        return self._deliver(resp, fmt, args)

    def scd_export_my_entries(self, args):
        fmt = args.get('format', 'md')
        return self._deliver(self.client().export_entries(fmt, **self._entry_filters(args)), fmt, args)

    def scd_create_entry(self, args):
        payload = {k: v for k, v in args.items() if k != 'period'}
        p = args.get('period')
        if p:
            start, end = period_range(p)
            payload.setdefault('period_start', start.isoformat())
            payload.setdefault('period_end', end.isoformat())
            payload.setdefault('period_kind', CREATE_PERIOD_KIND.get(p, 'custom'))
        if not payload.get('period_start') or not payload.get('period_end'):
            raise ToolError('Give period, or both period_start and period_end.')
        _check_date(payload, 'period_start', 'period_end')
        payload.setdefault('period_kind', 'week')
        for k in ('projects', 'categories', 'lab_priorities'):
            if k in payload:
                payload[k] = _list(payload[k])
        return {'json': self.client().create_entry(payload)}

    def scd_run_report(self, args):
        fmt = args.get('format', 'md')
        body = dict(self._report_filters(args), format=fmt)
        resp = self.client().request('POST', '/api/reports/', json_body=body)
        return self._deliver(resp, fmt, args)

    def scd_count_report(self, args):
        n = self.client().report_count(**self._report_filters(args))
        return {'json': {'count': n}}

    def scd_report_summary(self, args):
        fmt = args.get('format', 'md')
        f = self._report_filters(args)
        if fmt == 'json':
            data = self.client().summary('json', template=args.get('template'), **f)
            if args.get('output_path'):
                resp = scd_client.Response(200, {}, json.dumps(data, indent=2).encode())
                return self._deliver(resp, fmt, args)
            return {'json': data}
        return self._deliver(self.client().summary(fmt, template=args.get('template'), **f), fmt, args)

    # -- dispatch -------------------------------------------------------------

    def call(self, name, args):
        if name not in self.enabled_tools():
            raise ProtocolError(-32602, f'Unknown tool: {name}')
        if not isinstance(args, dict):
            raise ProtocolError(-32602, 'Tool arguments must be an object.')
        allowed = TOOLS[name]['inputSchema']['properties']
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise ToolError(f'Unknown argument(s) {", ".join(unknown)}. Allowed: {", ".join(allowed) or "none"}.')
        missing = [k for k in TOOLS[name]['inputSchema'].get('required', []) if args.get(k) in (None, '', [])]
        if missing:
            raise ToolError(f'Missing required argument(s): {", ".join(missing)}.')
        try:
            return getattr(self, name)(dict(args))
        except ScdApiError as exc:
            msg = str(exc)
            if exc.status == 401:
                msg += ('\nThe API token is missing, wrong or revoked. Rotate it at Profile > API Token '
                        'and update the scd-reporting config.')
            elif exc.status == 403:
                msg += '\nThe account behind this token does not have the role this needs (see scd_whoami).'
            raise ToolError(msg)
        except (ScdNetworkError, ScdError) as exc:
            raise ToolError(str(exc))


# ── JSON-RPC / MCP protocol ───────────────────────────────────────────────────

class McpServer:
    def __init__(self, runner, log=None):
        self.runner = runner
        self.protocol = LATEST_PROTOCOL
        self.log = log or (lambda msg: print(f'[scd-mcp] {msg}', file=sys.stderr, flush=True))

    def _tool_def(self, name):
        t = TOOLS[name]
        d = {'name': name, 'description': t['description'], 'inputSchema': t['inputSchema']}
        if self.protocol >= '2025-03-26':
            d['annotations'] = dict(t['annotations'], title=t['title'])
        if self.protocol >= '2025-06-18':
            d['title'] = t['title']
        return d

    def _result(self, out):
        if 'json' in out:
            data = out['json']
            text = json.dumps(data, indent=2, ensure_ascii=False)
            structured = data if isinstance(data, dict) else {'items': data}
        else:
            text, structured = out['text'], out.get('structured')
        result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
        if structured is not None and self.protocol >= '2025-06-18':
            result['structuredContent'] = structured
        return result

    def handle(self, msg):
        """Handle one decoded JSON-RPC message; return the response dict or None."""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            if isinstance(msg, dict) and 'method' not in msg and ('result' in msg or 'error' in msg):
                return None   # a response to a request we never send; ignore
            return self._error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'Invalid Request')
        method, mid, params = msg['method'], msg.get('id'), msg.get('params') or {}
        is_notification = 'id' not in msg
        try:
            result = self._dispatch(method, params)
        except ProtocolError as exc:
            return None if is_notification else self._error(mid, exc.code, exc.message)
        except Exception as exc:   # never let one bad request kill the server
            self.log(f'internal error in {method}: {exc}\n{traceback.format_exc()}')
            return None if is_notification else self._error(mid, -32603, f'Internal error: {exc}')
        if is_notification:
            return None
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}

    @staticmethod
    def _error(mid, code, message):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}

    def _dispatch(self, method, params):
        if method == 'initialize':
            requested = params.get('protocolVersion')
            self.protocol = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL
            client = (params.get('clientInfo') or {})
            self.log(f"initialize from {client.get('name', '?')} {client.get('version', '')}; "
                     f'requested {requested}, using {self.protocol}')
            if self.runner.settings_error:
                self.log(f'configuration error (tools will report it): {self.runner.settings_error}')
            info = {'name': SERVER_NAME, 'version': SERVER_VERSION}
            if self.protocol >= '2025-06-18':
                info['title'] = 'SCD Reporting'
            return {'protocolVersion': self.protocol,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': info,
                    'instructions': INSTRUCTIONS}
        if method == 'ping':
            return {}
        if method.startswith('notifications/'):
            return None
        if method == 'tools/list':
            return {'tools': [self._tool_def(n) for n in self.runner.enabled_tools()]}
        if method == 'tools/call':
            name = params.get('name')
            if not isinstance(name, str):
                raise ProtocolError(-32602, 'tools/call needs a tool name.')
            try:
                return self._result(self.runner.call(name, params.get('arguments') or {}))
            except ToolError as exc:
                return {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        raise ProtocolError(-32601, f'Method not found: {method}')

    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        self.log(f'scd-mcp {SERVER_VERSION} ready; tools: {", ".join(self.runner.enabled_tools())}')
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                reply = self._error(None, -32700, 'Parse error')
            else:
                if isinstance(msg, list):   # JSON-RPC batch (2025-03-26)
                    replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                    reply = replies or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False).encode('utf-8') + b'\n')
                stdout.flush()
        self.log('stdin closed; exiting')


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='scd-mcp', parents=[scd_client._connection_parser()],
        description='MCP server (stdio) exposing the SCD Reporting API as tools.')
    p.add_argument('--read-only', action='store_true', default=None,
                   help='do not offer scd_create_entry [SCD_MCP_READ_ONLY]')
    p.add_argument('--no-summary', action='store_true', default=None,
                   help='do not offer scd_report_summary (paid model calls) [SCD_MCP_NO_SUMMARY]')
    p.add_argument('--list-tools', action='store_true',
                   help='print the enabled tools and exit (no server)')
    args = p.parse_args(argv)
    runner = ToolRunner({
        'config': args.config, 'url': args.url, 'token': args.token, 'token_file': args.token_file,
        'timeout': args.timeout, 'ca_bundle': args.ca_bundle, 'insecure': args.insecure,
        'mcp_read_only': args.read_only, 'mcp_no_summary': args.no_summary,
    })
    if args.list_tools:
        if runner.settings_error:
            print(f'warning: {runner.settings_error}', file=sys.stderr)
        for name in runner.enabled_tools():
            print(f'{name:<24} {TOOLS[name]["title"]}')
        return 0
    try:
        McpServer(runner).serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
