"""Tests for the token-authenticated read API: reports, summary, entries,
whoami and taxonomy."""
import json
from datetime import date

import pytest
from django.urls import reverse

from apps.accounts.models import APIToken, User
from apps.audit.models import AuditLogEntry
from apps.entries.models import WorkItem
from apps.reports.ai_summary import SummaryResult
from apps.taxonomy.models import Category, Project, Tag, WorkGroup


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _user(username, role=User.Role.USER, **kw):
    return User.objects.create_user(username=username, email=f'{username}@example.com',
                                    password='pass', role=role, **kw)


def _auth(user):
    return {'HTTP_AUTHORIZATION': f'Bearer {APIToken.rotate(user).key}'}


def _entry(author, title, project, category, start=date(2026, 10, 5), end=date(2026, 10, 11), **kw):
    item = WorkItem.objects.create(author=author, title=title, description=f'{title} body',
                                   period_kind='week', period_start=start, period_end=end, **kw)
    item.projects.set([project])
    item.categories.set([category])
    return item


@pytest.fixture
def tax(db):
    return {
        'dune': Project.objects.create(name='DUNE', slug='dune'),
        'nova': Project.objects.create(name='NOvA', slug='nova'),
        'sci': Category.objects.create(name='Scientific', slug='scientific'),
        'g1': WorkGroup.objects.create(name='Group One', slug='g1'),
        'g2': WorkGroup.objects.create(name='Group Two', slug='g2'),
    }


@pytest.fixture
def people(db, tax):
    return {
        'alice': _user('alice', group=tax['g1']),
        'bob': _user('bob', group=tax['g2']),
        'auditor': _user('auditor', role=User.Role.AUDITOR),
        'leader': _user('leader', role=User.Role.GROUP_LEADER, group=tax['g1']),
        'head': _user('head', role=User.Role.DIVISION_HEAD),
    }


@pytest.fixture
def entries(db, tax, people):
    return {
        'a1': _entry(people['alice'], 'Alice DUNE work', tax['dune'], tax['sci']),
        'a2': _entry(people['alice'], 'Alice NOvA work', tax['nova'], tax['sci'],
                     start=date(2026, 9, 1), end=date(2026, 9, 7)),
        'b1': _entry(people['bob'], 'Bob DUNE work', tax['dune'], tax['sci']),
        'dho': _entry(people['bob'], 'Secret head note', tax['dune'], tax['sci'],
                      is_division_head_only=True),
        'arch': _entry(people['alice'], 'Alice archived', tax['dune'], tax['sci'], is_archived=True),
    }


# ── Authentication ────────────────────────────────────────────────────────────

class TestTokenAuth:
    @pytest.mark.parametrize('name', ['api-whoami', 'api-reports', 'api-taxonomy', 'api-entry-create'])
    def test_missing_token_is_401(self, client, db, name):
        assert client.get(reverse(name)).status_code == 401

    def test_bad_token_is_401(self, client, db):
        resp = client.get(reverse('api-whoami'), HTTP_AUTHORIZATION='Bearer nope')
        assert resp.status_code == 401

    def test_session_login_is_not_accepted(self, client, people):
        client.force_login(people['auditor'])
        assert client.get(reverse('api-reports')).status_code == 401

    def test_inactive_user_token_rejected(self, client, people):
        headers = _auth(people['alice'])
        people['alice'].is_active = False
        people['alice'].save()
        assert client.get(reverse('api-whoami'), **headers).status_code == 401

    def test_token_records_last_used(self, client, people):
        headers = _auth(people['alice'])
        client.get(reverse('api-whoami'), **headers)
        assert APIToken.objects.get(user=people['alice']).last_used_at is not None

    def test_method_not_allowed_is_json(self, client, people):
        resp = client.get(reverse('api-reports-summary'), **_auth(people['auditor']))
        assert resp.status_code == 405
        assert 'POST' in resp['Allow']
        assert 'error' in resp.json()


# ── Reports ───────────────────────────────────────────────────────────────────

class TestReportAPI:
    def test_regular_user_forbidden(self, client, people):
        assert client.get(reverse('api-reports'), **_auth(people['alice'])).status_code == 403

    def test_markdown_export(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'format': 'md'}, **_auth(people['auditor']))
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('text/markdown')
        body = resp.content.decode()
        assert '## Alice DUNE work' in body
        assert '## Bob DUNE work' in body
        # Auditor never sees division-head-only entries; archived are excluded by default.
        assert 'Secret head note' not in body
        assert 'Alice archived' not in body

    def test_default_format_is_json(self, client, people, entries):
        resp = client.get(reverse('api-reports'), **_auth(people['auditor']))
        assert resp.status_code == 200
        titles = {r['title'] for r in json.loads(resp.content)}
        assert titles == {'Alice DUNE work', 'Alice NOvA work', 'Bob DUNE work'}

    def test_project_slug_filter(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'format': 'json', 'project': 'nova'},
                          **_auth(people['auditor']))
        assert [r['title'] for r in json.loads(resp.content)] == ['Alice NOvA work']

    def test_comma_separated_slugs(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'projects': 'nova,dune'}, **_auth(people['auditor']))
        assert len(json.loads(resp.content)) == 3

    def test_unknown_slug_is_400(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'projects': 'atlas'}, **_auth(people['auditor']))
        assert resp.status_code == 400
        assert 'projects' in resp.json()['errors']

    def test_invalid_date_is_400(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'period_after': '2026-13-01'},
                          **_auth(people['auditor']))
        assert resp.status_code == 400
        assert 'period_after' in resp.json()['errors']

    def test_unknown_format_is_400(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'format': 'docx'}, **_auth(people['auditor']))
        assert resp.status_code == 400
        assert 'format' in resp.json()['errors']

    def test_date_window_contained_vs_overlap(self, client, people, entries):
        h = _auth(people['auditor'])
        window = {'period_after': '2026-10-06', 'period_before': '2026-10-31', 'count': '1'}
        assert client.get(reverse('api-reports'), window, **h).json()['count'] == 0
        window['date_match'] = 'overlap'
        assert client.get(reverse('api-reports'), window, **h).json()['count'] == 2

    def test_count_only(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'count': '1'}, **_auth(people['auditor']))
        assert resp.json() == {'count': 3}

    def test_show_archived(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'show_archived': 'true', 'format': 'json'},
                          **_auth(people['auditor']))
        assert [r['title'] for r in json.loads(resp.content)] == ['Alice archived']

    def test_json_post_body(self, client, people, entries):
        resp = client.post(reverse('api-reports'),
                           data=json.dumps({'format': 'json', 'projects': ['dune'],
                                            'author': 'bob'}),
                           content_type='application/json', **_auth(people['auditor']))
        assert resp.status_code == 200
        assert [r['title'] for r in json.loads(resp.content)] == ['Bob DUNE work']

    def test_selected_ids(self, client, people, entries):
        resp = client.get(reverse('api-reports'), {'ids': f"{entries['a1'].pk},{entries['b1'].pk}"},
                          **_auth(people['auditor']))
        assert {r['id'] for r in json.loads(resp.content)} == {entries['a1'].pk, entries['b1'].pk}

    def test_group_leader_scoped_to_own_group(self, client, people, entries):
        resp = client.get(reverse('api-reports'), **_auth(people['leader']))
        titles = {r['title'] for r in json.loads(resp.content)}
        assert titles == {'Alice DUNE work', 'Alice NOvA work'}

    def test_division_head_sees_dho_entries(self, client, people, entries, tax):
        people['head'].managed_groups.set([tax['g2']])
        resp = client.get(reverse('api-reports'), **_auth(people['head']))
        titles = {r['title'] for r in json.loads(resp.content)}
        assert titles == {'Bob DUNE work', 'Secret head note'}

    def test_export_is_audited(self, client, people, entries):
        client.get(reverse('api-reports'), {'format': 'csv'}, **_auth(people['auditor']))
        log = AuditLogEntry.objects.filter(action='export').latest('pk')
        assert log.actor == people['auditor']
        assert log.changes['source'] == 'api'
        assert log.changes['format'] == 'csv'
        assert log.changes['count'] == 3

    @pytest.mark.parametrize('fmt,ctype', [
        ('txt', 'text/plain'), ('csv', 'text/csv'), ('pdf', 'application/pdf'),
        ('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    ])
    def test_every_format(self, client, people, entries, fmt, ctype):
        resp = client.get(reverse('api-reports'), {'format': fmt}, **_auth(people['auditor']))
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith(ctype)


# ── AI summary ────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_summary(monkeypatch):
    calls = []

    def generate(qs, user=None, template=None):
        calls.append({'count': qs.count(), 'user': user, 'template': template})
        return SummaryResult(text='# Summary\n\nAll good.', truncated=False, stop_reason='end_turn',
                             input_tokens=100, output_tokens=10, max_tokens=4096)

    monkeypatch.setattr('apps.reports.ai_summary.generate', generate)
    return calls


class TestSummaryAPI:
    def test_json_summary(self, client, people, entries, fake_summary):
        resp = client.post(reverse('api-reports-summary'), data=json.dumps({'projects': ['dune']}),
                           content_type='application/json', **_auth(people['auditor']))
        assert resp.status_code == 200
        data = resp.json()
        assert data['text'].startswith('# Summary')
        assert data['count'] == 2
        assert fake_summary[0]['count'] == 2

    def test_markdown_summary(self, client, people, entries, fake_summary):
        resp = client.post(reverse('api-reports-summary'), {'format': 'md'}, **_auth(people['auditor']))
        assert resp['Content-Type'].startswith('text/markdown')
        assert resp.content.decode() == '# Summary\n\nAll good.'

    def test_no_match_is_400(self, client, people, entries, fake_summary):
        resp = client.post(reverse('api-reports-summary'), {'search': 'zzzz-nothing'},
                           **_auth(people['auditor']))
        assert resp.status_code == 400
        assert not fake_summary

    def test_unknown_template_is_400(self, client, people, entries, fake_summary):
        resp = client.post(reverse('api-reports-summary'), {'template': 'missing'},
                           **_auth(people['auditor']))
        assert resp.status_code == 400
        assert 'template' in resp.json()['errors']

    def test_config_error_is_400(self, client, people, entries, monkeypatch):
        def boom(*a, **k):
            raise ValueError('ANTHROPIC_API_KEY is not set.')
        monkeypatch.setattr('apps.reports.ai_summary.generate', boom)
        resp = client.post(reverse('api-reports-summary'), **_auth(people['auditor']))
        assert resp.status_code == 400
        assert 'ANTHROPIC_API_KEY' in resp.json()['error']

    def test_regular_user_forbidden(self, client, people, entries, fake_summary):
        resp = client.post(reverse('api-reports-summary'), **_auth(people['alice']))
        assert resp.status_code == 403


# ── Entries ───────────────────────────────────────────────────────────────────

class TestEntryListAPI:
    def test_lists_only_own_unarchived(self, client, people, entries):
        resp = client.get(reverse('api-entry-create'), **_auth(people['alice']))
        assert resp.status_code == 200
        data = resp.json()
        assert data['count'] == 2
        # Newest period first.
        assert [r['title'] for r in data['results']] == ['Alice DUNE work', 'Alice NOvA work']
        first = data['results'][0]
        assert first['projects'] == [{'id': entries['a1'].projects.get().pk, 'slug': 'dune', 'name': 'DUNE'}]
        assert first['url'].endswith(f"/entries/{entries['a1'].pk}/")

    def test_pagination(self, client, people, entries):
        resp = client.get(reverse('api-entry-create'), {'limit': 1, 'offset': 1}, **_auth(people['alice']))
        data = resp.json()
        assert data['count'] == 2 and data['limit'] == 1 and data['offset'] == 1
        assert [r['title'] for r in data['results']] == ['Alice NOvA work']

    def test_bad_limit_is_400(self, client, people, entries):
        resp = client.get(reverse('api-entry-create'), {'limit': 5000}, **_auth(people['alice']))
        assert resp.status_code == 400

    def test_filters(self, client, people, entries):
        h = _auth(people['alice'])
        url = reverse('api-entry-create')
        assert client.get(url, {'project': 'nova'}, **h).json()['count'] == 1
        assert client.get(url, {'q': 'dune'}, **h).json()['count'] == 1
        assert client.get(url, {'period_after': '2026-10-01'}, **h).json()['count'] == 1
        assert client.get(url, {'archived': '1'}, **h).json()['count'] == 1
        assert client.get(url, {'archived': 'all'}, **h).json()['count'] == 3

    def test_tag_search(self, client, people, entries):
        entries['a2'].tags.set([Tag.objects.create(name='timing')])
        resp = client.get(reverse('api-entry-create'), {'q': 'timing'}, **_auth(people['alice']))
        assert [r['title'] for r in resp.json()['results']] == ['Alice NOvA work']

    def test_markdown_export_of_own_entries(self, client, people, entries):
        resp = client.get(reverse('api-entry-create'), {'format': 'md'}, **_auth(people['alice']))
        body = resp.content.decode()
        assert resp['Content-Type'].startswith('text/markdown')
        assert '## Alice DUNE work' in body and 'Bob' not in body


class TestEntryDetailAPI:
    def test_own_entry(self, client, people, entries):
        resp = client.get(reverse('api-entry-detail', args=[entries['a1'].pk]), **_auth(people['alice']))
        assert resp.status_code == 200
        assert resp.json()['title'] == 'Alice DUNE work'

    def test_other_users_entry_is_404_for_regular_user(self, client, people, entries):
        resp = client.get(reverse('api-entry-detail', args=[entries['b1'].pk]), **_auth(people['alice']))
        assert resp.status_code == 404

    def test_auditor_sees_others_but_not_dho(self, client, people, entries):
        h = _auth(people['auditor'])
        assert client.get(reverse('api-entry-detail', args=[entries['b1'].pk]), **h).status_code == 200
        assert client.get(reverse('api-entry-detail', args=[entries['dho'].pk]), **h).status_code == 404

    def test_markdown_format(self, client, people, entries):
        resp = client.get(reverse('api-entry-detail', args=[entries['a1'].pk]), {'format': 'md'},
                          **_auth(people['alice']))
        assert resp.content.decode().startswith('## Alice DUNE work')


# ── Discovery ─────────────────────────────────────────────────────────────────

class TestWhoAmITaxonomy:
    def test_whoami_regular(self, client, people):
        data = client.get(reverse('api-whoami'), **_auth(people['alice'])).json()
        assert data['email'] == 'alice@example.com'
        assert data['role'] == 'user'
        assert data['can_report'] is False
        assert data['group']['slug'] == 'g1'
        assert 'md' in data['export_formats']

    def test_whoami_group_leader_scope(self, client, people):
        data = client.get(reverse('api-whoami'), **_auth(people['leader'])).json()
        assert data['can_report'] is True
        assert [g['slug'] for g in data['report_group_scope']] == ['g1']
        assert data['report_project_scope'] is None

    def test_taxonomy_all(self, client, people, tax):
        Project.objects.create(name='Old', slug='old', is_active=False)
        data = client.get(reverse('api-taxonomy'), **_auth(people['alice'])).json()
        assert {p['slug'] for p in data['projects']} == {'dune', 'nova'}
        assert set(data) == {'projects', 'categories', 'entry-types', 'groups', 'lab-priorities', 'tags'}

    def test_taxonomy_kind_with_inactive(self, client, people, tax):
        Project.objects.create(name='Old', slug='old', is_active=False)
        data = client.get(reverse('api-taxonomy-kind', args=['projects']), {'all': '1'},
                          **_auth(people['alice'])).json()
        assert {p['slug'] for p in data['projects']} == {'dune', 'nova', 'old'}

    def test_taxonomy_unknown_kind(self, client, people):
        resp = client.get(reverse('api-taxonomy-kind', args=['planets']), **_auth(people['alice']))
        assert resp.status_code == 404
        assert 'projects' in resp.json()['kinds']


def test_api_docs_page_lists_new_endpoints(client, people):
    client.force_login(people['alice'])
    resp = client.get(reverse('api'))
    assert resp.status_code == 200
    body = resp.content.decode()
    for path in ('/api/reports/', '/api/reports/summary/', '/api/whoami/', '/api/taxonomy/'):
        assert path in body
