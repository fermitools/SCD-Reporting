"""Tests for the reports app: access control, preview, and all five exporters."""
import csv
import io
import json
from datetime import date, timedelta

import openpyxl
import pytest

from django.urls import reverse

from apps.accounts.models import User
from apps.entries.models import WorkItem
from apps.taxonomy.models import Category, EntryType, Project, WorkGroup


def _new_entry(project=None, category=None, lab_priority=None, **kwargs):
    """Create a WorkItem and set its project/category/lab_priority M2M relations."""
    item = WorkItem.objects.create(**kwargs)
    if project is not None:
        item.projects.set(project if isinstance(project, (list, tuple)) else [project])
    if category is not None:
        item.categories.set(category if isinstance(category, (list, tuple)) else [category])
    if lab_priority is not None:
        item.lab_priorities.set(lab_priority if isinstance(lab_priority, (list, tuple)) else [lab_priority])
    return item


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username='admin', email='admin@example.com', password='pass',
        role=User.Role.ADMIN,
    )


@pytest.fixture
def auditor_user(db):
    return User.objects.create_user(
        username='auditor', email='auditor@example.com', password='pass',
        role=User.Role.AUDITOR,
    )


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(
        username='user', email='user@example.com', password='pass',
    )


@pytest.fixture
def project(db):
    return Project.objects.create(name='DUNE', slug='dune')


@pytest.fixture
def category(db):
    return Category.objects.create(name='Scientific', slug='scientific')


@pytest.fixture
def entry(db, regular_user, project, category):
    today = date.today()
    start = today - timedelta(days=today.weekday())
    return _new_entry(
        author=regular_user,
        title='Test entry',
        project=project,
        category=category,
        period_kind='week',
        period_start=start,
        period_end=start + timedelta(days=6),
        description='Some work done.',
    )


# ── Access control ────────────────────────────────────────────────────────────

class TestReportAccess:
    def test_anonymous_redirected(self, client):
        assert client.get(reverse('reports:index')).status_code == 302

    def test_regular_user_forbidden(self, client, regular_user):
        client.force_login(regular_user)
        assert client.get(reverse('reports:index')).status_code == 403

    def test_auditor_can_access(self, client, auditor_user):
        client.force_login(auditor_user)
        assert client.get(reverse('reports:index')).status_code == 200

    def test_admin_can_access(self, client, admin_user):
        client.force_login(admin_user)
        assert client.get(reverse('reports:index')).status_code == 200


# ── Preview ───────────────────────────────────────────────────────────────────

class TestReportPreview:
    def test_empty_filters_returns_all(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = client.post(reverse('reports:preview'), {})
        assert resp.status_code == 200
        assert b'Test entry' in resp.content

    def test_project_filter(self, client, admin_user, entry, project, category, db):
        other = Project.objects.create(name='CMS', slug='cms')
        client.force_login(admin_user)
        resp = client.post(reverse('reports:preview'), {'projects': [other.pk]})
        assert b'Test entry' not in resp.content

    def test_projects_filter_matches_any(self, client, admin_user, entry, project, category, db):
        p2 = Project.objects.create(name='CMS', slug='cms')
        today = date.today()
        start = today - timedelta(days=today.weekday())
        _new_entry(
            project=p2, category=category, author=entry.author, title='Second entry',
            period_kind='week', period_start=start, period_end=start + timedelta(days=6),
            description='x',
        )
        client.force_login(admin_user)
        # Selecting both projects returns entries matching EITHER (OR semantics).
        resp = client.post(reverse('reports:preview'), {'projects': [project.pk, p2.pk]})
        body = resp.content.decode()
        assert 'Test entry' in body
        assert 'Second entry' in body
        # Selecting only the second project excludes the first entry.
        resp = client.post(reverse('reports:preview'), {'projects': [p2.pk]})
        body = resp.content.decode()
        assert 'Test entry' not in body
        assert 'Second entry' in body

    def test_author_email_filter(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = client.post(reverse('reports:preview'), {'author_email': 'nomatch@x.com'})
        assert b'Test entry' not in resp.content

    def test_entry_type_filter(self, client, admin_user, entry, db):
        weekly = EntryType.objects.create(name='Weekly Report', slug='weekly-report')
        milestone = EntryType.objects.create(name='Milestone', slug='milestone')
        entry.entry_type = weekly
        entry.save(update_fields=['entry_type'])
        client.force_login(admin_user)
        # Matching type shows the entry…
        resp = client.post(reverse('reports:preview'), {'entry_type': weekly.pk})
        assert b'Test entry' in resp.content
        # …a different type filters it out.
        resp = client.post(reverse('reports:preview'), {'entry_type': milestone.pk})
        assert b'Test entry' not in resp.content

    def test_no_match_shows_empty_message(self, client, admin_user):
        client.force_login(admin_user)
        resp = client.post(reverse('reports:preview'), {'author_email': 'nobody@nowhere.com'})
        assert b'No entries matched' in resp.content


# ── Exporters ─────────────────────────────────────────────────────────────────

class TestExporters:
    def _download(self, client, fmt, extra_data=None):
        data = extra_data or {}
        return client.post(reverse('reports:download', kwargs={'fmt': fmt}), data)

    def test_txt_download(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = self._download(client, 'txt')
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('text/plain')
        assert b'Test entry' in resp.content

    def test_csv_download(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = self._download(client, 'csv')
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('text/csv')
        assert b'Test entry' in resp.content
        assert b'title' in resp.content  # header row

    def test_json_download(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = self._download(client, 'json')
        assert resp.status_code == 200
        data = json.loads(resp.content)
        assert isinstance(data, list)
        assert any(r['title'] == 'Test entry' for r in data)

    def test_xlsx_download(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = self._download(client, 'xlsx')
        assert resp.status_code == 200
        assert 'spreadsheetml' in resp['Content-Type']
        # XLSX magic bytes: PK header
        assert resp.content[:2] == b'PK'

    def test_csv_escapes_spreadsheet_formulas(self, client, admin_user, entry):
        entry.title = '=IMPORTXML("https://example.com")'
        entry.description = '@SUM(1,1)'
        entry.save()
        client.force_login(admin_user)

        resp = self._download(client, 'csv')

        rows = list(csv.DictReader(io.StringIO(resp.content.decode())))
        assert rows[0]['title'] == '\'=IMPORTXML("https://example.com")'
        assert rows[0]['description'] == "'@SUM(1,1)"

    def test_xlsx_escapes_spreadsheet_formulas(self, client, admin_user, entry):
        entry.title = '=HYPERLINK("https://example.com")'
        entry.description = '+SUM(1,1)'
        entry.save()
        client.force_login(admin_user)

        resp = self._download(client, 'xlsx')

        wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=False)
        ws = wb.active
        assert ws['C2'].value == '\'=HYPERLINK("https://example.com")'
        assert ws['O2'].value == "'+SUM(1,1)"

    def test_non_spreadsheet_formats_preserve_text_content(self, client, admin_user, entry):
        entry.title = '=Plain text title'
        entry.description = '@Plain text description'
        entry.save()
        client.force_login(admin_user)

        txt_resp = self._download(client, 'txt')
        json_resp = self._download(client, 'json')
        pdf_resp = self._download(client, 'pdf')

        assert b'=Plain text title' in txt_resp.content
        data = json.loads(json_resp.content)
        assert data[0]['title'] == '=Plain text title'
        assert data[0]['description'] == '@Plain text description'
        assert pdf_resp.status_code == 200
        assert pdf_resp.content[:4] == b'%PDF'

    def test_unknown_format_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        resp = self._download(client, 'docx')
        assert resp.status_code == 400

    def test_download_respects_filter(self, client, admin_user, entry):
        client.force_login(admin_user)
        resp = self._download(client, 'json', {'author_email': 'nobody@nowhere.com'})
        data = json.loads(resp.content)
        assert data == []


# ── Group scope filtering (issue #10) ─────────────────────────────────────────

class TestGroupScopeFiltering:
    """
    Group-scoped reports should use WorkItem.group as the authoritative dimension.
    Fallback to author.group only when the entry has no explicit group set.
    """

    def _make_entry(self, author, project, category, group=None):
        today = date.today()
        start = today - timedelta(days=today.weekday())
        return _new_entry(
            author=author,
            title=f'Entry by {author.email}',
            project=project,
            category=category,
            period_kind='week',
            period_start=start,
            period_end=start + timedelta(days=6),
            description='desc',
            group=group,
        )

    def test_entry_assigned_to_leaders_group_is_visible(self, client, db, project, category):
        group_a = WorkGroup.objects.create(name='Group A', slug='group-a')
        group_b = WorkGroup.objects.create(name='Group B', slug='group-b')

        leader = User.objects.create_user(username='leader', email='leader@x.com', password='p',
                                          role=User.Role.GROUP_LEADER, group=group_a)
        outsider = User.objects.create_user(username='out', email='out@x.com', password='p',
                                            group=group_b)
        # Entry is explicitly assigned to group_a even though the author is in group_b
        entry = self._make_entry(outsider, project, category, group=group_a)

        client.force_login(leader)
        resp = client.post(reverse('reports:preview'), {})
        assert resp.status_code == 200
        assert entry.title.encode() in resp.content

    def test_entry_assigned_to_other_group_is_hidden(self, client, db, project, category):
        group_a = WorkGroup.objects.create(name='Group A', slug='group-a')
        group_b = WorkGroup.objects.create(name='Group B', slug='group-b')

        leader = User.objects.create_user(username='leader2', email='leader2@x.com', password='p',
                                          role=User.Role.GROUP_LEADER, group=group_a)
        member = User.objects.create_user(username='mem', email='mem@x.com', password='p',
                                          group=group_a)
        # Entry is explicitly assigned to group_b — should NOT appear for leader of group_a
        entry = self._make_entry(member, project, category, group=group_b)

        client.force_login(leader)
        resp = client.post(reverse('reports:preview'), {})
        assert resp.status_code == 200
        assert entry.title.encode() not in resp.content

    def test_ungrouped_entry_visible_by_author_group(self, client, db, project, category):
        group_a = WorkGroup.objects.create(name='Group A', slug='group-a')

        leader = User.objects.create_user(username='leader3', email='leader3@x.com', password='p',
                                          role=User.Role.GROUP_LEADER, group=group_a)
        member = User.objects.create_user(username='mem2', email='mem2@x.com', password='p',
                                          group=group_a)
        # Entry has no explicit group — falls back to author.group
        entry = self._make_entry(member, project, category, group=None)

        client.force_login(leader)
        resp = client.post(reverse('reports:preview'), {})
        assert resp.status_code == 200
        assert entry.title.encode() in resp.content


class TestNamedTemplateDelete:
    def _make_template(self, user):
        from apps.reports.models import NamedPromptTemplate
        return NamedPromptTemplate.objects.create(
            user=user, name='My Template',
            system_prompt='sys', user_template='tmpl {entries}',
        )

    def test_user_can_delete_own_template(self, client, auditor_user):
        tpl = self._make_template(auditor_user)
        client.force_login(auditor_user)
        resp = client.post(reverse('reports:prompt-template-delete', kwargs={'pk': tpl.pk}))
        assert resp.status_code == 302
        from apps.reports.models import NamedPromptTemplate
        assert not NamedPromptTemplate.objects.filter(pk=tpl.pk).exists()

    def test_user_cannot_delete_others_template(self, client, auditor_user, admin_user):
        tpl = self._make_template(admin_user)
        client.force_login(auditor_user)
        resp = client.post(reverse('reports:prompt-template-delete', kwargs={'pk': tpl.pk}))
        assert resp.status_code == 404

    def test_admin_page_lists_templates(self, client, admin_user, auditor_user):
        self._make_template(auditor_user)
        client.force_login(admin_user)
        resp = client.get(reverse('reports:prompt-templates-admin'))
        assert resp.status_code == 200
        assert b'My Template' in resp.content
        assert b'auditor@example.com' in resp.content

    def test_admin_can_delete_any_template(self, client, admin_user, auditor_user):
        tpl = self._make_template(auditor_user)
        client.force_login(admin_user)
        resp = client.post(reverse('reports:prompt-template-admin-delete', kwargs={'pk': tpl.pk}))
        assert resp.status_code == 302
        from apps.reports.models import NamedPromptTemplate
        assert not NamedPromptTemplate.objects.filter(pk=tpl.pk).exists()

    def test_non_admin_cannot_access_admin_page(self, client, auditor_user):
        client.force_login(auditor_user)
        resp = client.get(reverse('reports:prompt-templates-admin'))
        assert resp.status_code == 403


def _summary_result(text, truncated=False, max_tokens=16000):
    from apps.reports.ai_summary import SummaryResult
    return SummaryResult(
        text=text, truncated=truncated, stop_reason='max_tokens' if truncated else 'end_turn',
        input_tokens=100, output_tokens=200, max_tokens=max_tokens,
    )


class TestReportSummary:
    def test_ai_summary_markdown_is_sanitized(self, client, admin_user, entry, monkeypatch):
        def fake_generate(qs, **kwargs):
            return _summary_result(
                '# Summary\n\n<img src=x onerror=alert(1)> **safe** [bad](javascript:alert(1))'
            )

        monkeypatch.setattr('apps.reports.views.ai_summary.generate', fake_generate)
        client.force_login(admin_user)

        resp = client.post(reverse('reports:summary'), {})

        assert resp.status_code == 200
        assert b'<h1>Summary</h1>' in resp.content
        assert b'<strong>safe</strong>' in resp.content
        assert b'<img' not in resp.content
        assert b'<a rel="noopener noreferrer">bad</a>' in resp.content
        assert b'<a href="javascript:' not in resp.content

    def test_truncated_summary_is_flagged_in_the_page(self, client, admin_user, entry, monkeypatch):
        """A summary cut off at max_tokens must say so (the reported symptom).

        The old code returned the partial text with no indication, so a report
        that stopped after a few sections looked like a complete one.
        """
        monkeypatch.setattr(
            'apps.reports.views.ai_summary.generate',
            lambda qs, **kw: _summary_result('## Overview\n\nPartial', truncated=True),
        )
        client.force_login(admin_user)
        resp = client.post(reverse('reports:summary'), {})

        assert b'This summary is incomplete' in resp.content
        assert b'16000' in resp.content
        assert b'Partial' in resp.content     # the partial text is still shown

    def test_complete_summary_has_no_truncation_notice(self, client, admin_user, entry, monkeypatch):
        monkeypatch.setattr(
            'apps.reports.views.ai_summary.generate',
            lambda qs, **kw: _summary_result('## Overview\n\nAll of it'),
        )
        client.force_login(admin_user)
        resp = client.post(reverse('reports:summary'), {})
        assert b'This summary is incomplete' not in resp.content

    def test_truncation_is_recorded_in_the_audit_log(self, client, admin_user, entry, monkeypatch):
        from apps.audit.models import AuditLogEntry
        monkeypatch.setattr(
            'apps.reports.views.ai_summary.generate',
            lambda qs, **kw: _summary_result('partial', truncated=True),
        )
        client.force_login(admin_user)
        client.post(reverse('reports:summary'), {})
        row = AuditLogEntry.objects.filter(action='export').order_by('-timestamp').first()
        assert row.changes['truncated'] is True
        assert row.changes['output_tokens'] == 200


class TestSummaryTokenBudget:
    """The truncation bug itself: max_tokens was pinned at 2048 (GitHub follow-up).

    The default prompt asks for a Markdown table row per entry, so a report over
    a few dozen entries exceeded 2048 output tokens and stopped mid-section.
    """

    def test_max_tokens_default_is_not_the_old_ceiling(self):
        from django.conf import settings
        assert settings.ANTHROPIC_MAX_TOKENS >= 16000

    def test_input_limit_clears_a_full_history_summary(self):
        """The guard must not refuse what the model can actually accept.

        Production's full non-archived history is ~325k input tokens against a
        1M-context model, so a limit below that would turn a working (if
        output-truncated) summary into a hard error.
        """
        from django.conf import settings
        assert settings.ANTHROPIC_MAX_INPUT_TOKENS >= 400000

    def test_generate_passes_the_configured_max_tokens_and_streams(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'test-key'
        settings.ANTHROPIC_MAX_TOKENS = 24000
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0      # skip the pre-count round trip
        captured = {}

        stub = _FakeAnthropic(captured, stop_reason='end_turn', text='done')
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        result = ai_summary.generate(WorkItem.objects.all())

        assert captured['streamed'] is True, 'a large max_tokens must be streamed'
        assert captured['max_tokens'] == 24000
        assert result.text == 'done'
        assert result.truncated is False

    def test_generate_reports_truncation(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'test-key'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        stub = _FakeAnthropic({}, stop_reason='max_tokens', text='cut off here')
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        result = ai_summary.generate(WorkItem.objects.all())

        assert result.truncated is True
        assert result.stop_reason == 'max_tokens'
        assert result.text == 'cut off here'

    def test_oversized_input_is_refused_with_an_actionable_message(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'test-key'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 10
        stub = _FakeAnthropic({}, stop_reason='end_turn', text='x', counted_tokens=5000)
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        with pytest.raises(ValueError, match='input tokens'):
            ai_summary.generate(WorkItem.objects.all())

    def test_a_failed_pre_count_does_not_block_the_summary(self, db, entry, settings, monkeypatch):
        """The guard is a courtesy; losing it must not lose the feature."""
        settings.ANTHROPIC_API_KEY = 'test-key'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 150000
        stub = _FakeAnthropic({}, stop_reason='end_turn', text='ok', count_raises=True)
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        assert ai_summary.generate(WorkItem.objects.all()).text == 'ok'

    def test_missing_api_key_raises_before_any_call(self, db, entry, settings):
        settings.ANTHROPIC_API_KEY = ''
        from apps.reports import ai_summary
        with pytest.raises(ValueError, match='ANTHROPIC_API_KEY'):
            ai_summary.generate(WorkItem.objects.all())


class _FakeAnthropic:
    """Minimal stand-in for anthropic.Anthropic covering the calls generate() makes."""

    def __init__(self, captured, stop_reason, text, counted_tokens=10, count_raises=False):
        self._captured = captured
        self._stop_reason = stop_reason
        self._text = text
        self._counted = counted_tokens
        self._count_raises = count_raises
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def count_tokens(self, **kwargs):
            if self._outer._count_raises:
                raise RuntimeError('count_tokens unavailable')
            return type('Count', (), {'input_tokens': self._outer._counted})()

        def create(self, **kwargs):
            self._outer._captured['streamed'] = False
            raise AssertionError('generate() must stream, not block')

        def stream(self, **kwargs):
            self._outer._captured['streamed'] = True
            self._outer._captured['max_tokens'] = kwargs.get('max_tokens')
            self._outer._captured['model'] = kwargs.get('model')
            return self._outer._Stream(self._outer)

    class _Stream:
        def __init__(self, outer):
            self._outer = outer

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            outer = self._outer
            block = type('Block', (), {'type': 'text', 'text': outer._text})()
            usage = type('Usage', (), {'input_tokens': 10, 'output_tokens': 20})()
            return type('Msg', (), {
                'content': [block], 'stop_reason': outer._stop_reason, 'usage': usage,
            })()


# ── PDF markdown tables (issue #13) ──────────────────────────────────────────

class TestMdToPdfTables:
    def test_cells_are_wrapping_paragraphs_within_frame(self):
        from reportlab.platypus import Paragraph
        from apps.reports._pdf import _build_table, _s
        rows = [['Title', 'Author', 'Cat'],
                ['A very long report title that must wrap ' * 4, 'someone@example.com', 'Ops']]
        t = _build_table(rows, 400.0, _s())
        assert all(isinstance(c, Paragraph) for row in t._cellvalues for c in row)
        assert abs(sum(t._colWidths) - 400.0) < 1e-6
        assert t._colWidths[0] > t._colWidths[1] > t._colWidths[2]

    def test_ragged_rows_are_padded(self):
        from apps.reports._pdf import _build_table, _s
        t = _build_table([['a', 'b', 'c'], ['only-one']], 300.0, _s())
        assert all(len(row) == 3 for row in t._cellvalues)

    def test_inline_markdown_in_cells_is_escaped_and_styled(self):
        from apps.reports._pdf import _build_table, _s
        t = _build_table([['H'], ['**bold** <x>']], 200.0, _s())
        assert '<b>bold</b>' in t._cellvalues[1][0].text
        assert '&lt;x&gt;' in t._cellvalues[1][0].text

    def test_long_table_renders_to_pdf(self):
        from apps.reports._pdf import md_to_pdf
        md = '| Title | Author |\n|---|---|\n' + ''.join(
            f'| {"long title text " * 12} | user{i}@example.com |\n' for i in range(60))
        pdf = md_to_pdf(md, 'Summary', 'meta')
        assert pdf.startswith(b'%PDF')


# ── Date-range match mode (GitHub #30) ───────────────────────────────────────

class TestDateRangeMatchMode:
    """The requested window can either contain an entry's period or merely overlap it.

    Adam Lyon's report (#30): a query for 8/15–9/1 missed an entry covering
    8/1–8/31 because containment is the only comparison the filter offered.
    """

    @pytest.fixture
    def entries(self, db, regular_user, project, category):
        """Four entries around the window 2026-08-15 .. 2026-09-01."""
        def make(title, start, end):
            return _new_entry(
                author=regular_user, title=title, project=project, category=category,
                period_kind='custom', period_start=start, period_end=end,
                description='x',
            )
        return {
            'inside':        make('inside',        date(2026, 8, 16), date(2026, 8, 30)),
            'exact':         make('exact',         date(2026, 8, 15), date(2026, 9, 1)),
            'straddle_left': make('straddle_left', date(2026, 8, 1),  date(2026, 8, 31)),
            'straddle_right':make('straddle_right',date(2026, 8, 25), date(2026, 9, 8)),
            'before':        make('before',        date(2026, 7, 1),  date(2026, 7, 31)),
            'after':         make('after',         date(2026, 9, 2),  date(2026, 9, 8)),
        }

    WINDOW = {'period_after': '2026-08-15', 'period_before': '2026-09-01'}

    def _titles(self, client, extra=None):
        data = dict(self.WINDOW)
        data.update(extra or {})
        body = client.post(reverse('reports:preview'), data).content
        return {name for name in
                (b'inside', b'exact', b'straddle_left', b'straddle_right', b'before', b'after')
                if name in body}

    def test_contained_is_the_default(self, client, admin_user, entries):
        client.force_login(admin_user)
        assert self._titles(client) == {b'inside', b'exact'}

    def test_contained_can_be_requested_explicitly(self, client, admin_user, entries):
        client.force_login(admin_user)
        assert self._titles(client, {'date_match': 'contained'}) == {b'inside', b'exact'}

    def test_overlap_includes_straddling_periods(self, client, admin_user, entries):
        client.force_login(admin_user)
        titles = self._titles(client, {'date_match': 'overlap'})
        assert titles == {b'inside', b'exact', b'straddle_left', b'straddle_right'}

    def test_overlap_still_excludes_disjoint_periods(self, client, admin_user, entries):
        client.force_login(admin_user)
        titles = self._titles(client, {'date_match': 'overlap'})
        assert b'before' not in titles
        assert b'after' not in titles

    def test_overlap_with_only_a_lower_bound(self, client, admin_user, entries):
        """One-sided window: everything that had not finished by 2026-08-15."""
        client.force_login(admin_user)
        body = client.post(reverse('reports:preview'), {
            'period_after': '2026-08-15', 'date_match': 'overlap',
        }).content
        assert b'straddle_left' in body   # ends 8/31, so still open on 8/15
        assert b'before' not in body      # ended 7/31

    def test_overlap_with_only_an_upper_bound(self, client, admin_user, entries):
        client.force_login(admin_user)
        body = client.post(reverse('reports:preview'), {
            'period_before': '2026-09-01', 'date_match': 'overlap',
        }).content
        assert b'straddle_right' in body  # starts 8/25, so already open on 9/1
        assert b'after' not in body       # starts 9/2

    def test_mode_selector_is_rendered_on_the_index_page(self, client, admin_user):
        client.force_login(admin_user)
        body = client.get(reverse('reports:index')).content
        assert b'name="date_match"' in body
        assert b'Overlapping the range' in body


# ── Streamed AI summary over SSE ─────────────────────────────────────────────

class _FakeStreamingAnthropic(_FakeAnthropic):
    """_FakeAnthropic whose stream also yields text chunks via text_stream."""

    def __init__(self, captured, stop_reason, chunks, **kw):
        super().__init__(captured, stop_reason, ''.join(chunks), **kw)
        self._chunks = chunks

    class _Stream:
        def __init__(self, outer):
            self._outer = outer

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @property
        def text_stream(self):
            return iter(self._outer._chunks)

        def get_final_message(self):
            outer = self._outer
            block = type('Block', (), {'type': 'text', 'text': outer._text})()
            usage = type('Usage', (), {'input_tokens': 10, 'output_tokens': 20})()
            return type('Msg', (), {
                'content': [block], 'stop_reason': outer._stop_reason, 'usage': usage,
            })()


def _sse_frames(response):
    """Parse an SSE response body into a list of (event, payload) pairs."""
    body = b''.join(response.streaming_content).decode()
    out = []
    for raw in body.split('\n\n'):
        if not raw.strip():
            continue
        event, data = 'message', ''
        for line in raw.split('\n'):
            if line.startswith('event: '):
                event = line[7:]
            elif line.startswith('data: '):
                data += line[6:]
        out.append((event, json.loads(data)))
    return out


class TestSummaryStreaming:
    """The streamed endpoint exists so a long summary is not bounded by the
    300s gunicorn/route timeout that caps the blocking one."""

    def _stub(self, monkeypatch, chunks, stop_reason='end_turn'):
        stub = _FakeStreamingAnthropic({}, stop_reason, chunks)
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)
        return stub

    def test_requires_reporter_role(self, client, regular_user):
        client.force_login(regular_user)
        assert client.post(reverse('reports:summary-stream'), {}).status_code == 403

    def test_anonymous_is_redirected(self, client, db):
        assert client.post(reverse('reports:summary-stream'), {}).status_code == 302

    def test_content_type_and_buffering_headers(self, client, admin_user, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        self._stub(monkeypatch, ['ok'])
        client.force_login(admin_user)
        resp = client.post(reverse('reports:summary-stream'), {})
        assert resp['Content-Type'] == 'text/event-stream'
        assert resp['Cache-Control'] == 'no-cache'
        # A buffering proxy would deliver the whole stream at once.
        assert resp['X-Accel-Buffering'] == 'no'

    def test_chunks_arrive_as_delta_frames(self, client, admin_user, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        self._stub(monkeypatch, ['## Over', 'view\n\n', 'body text'])
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))

        events = [e for e, _ in frames]
        assert events[0] == 'start'
        assert events.count('delta') == 3
        assert events[-1] == 'done'
        streamed = ''.join(p['t'] for e, p in frames if e == 'delta')
        assert streamed == '## Overview\n\nbody text'

    def test_newlines_survive_the_wire_format(self, client, admin_user, entry, settings, monkeypatch):
        """SSE is newline-delimited, so payloads are JSON-encoded."""
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        self._stub(monkeypatch, ['line one\nline two\n\nline three'])
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))
        deltas = [p['t'] for e, p in frames if e == 'delta']
        assert deltas == ['line one\nline two\n\nline three']

    def test_done_frame_carries_the_rendered_sanitised_pane(self, client, admin_user, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        self._stub(monkeypatch, ['# Head\n\n<img src=x onerror=alert(1)> **safe**'])
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))
        done = [p for e, p in frames if e == 'done'][0]

        assert '<h1>Head</h1>' in done['html']
        assert '<strong>safe</strong>' in done['html']
        assert '<img' not in done['html']          # nh3 still owns sanitising
        assert 'summary_text' in done['html']      # download forms came back
        assert done['truncated'] is False

    def test_truncation_reaches_the_done_frame_and_the_pane(self, client, admin_user, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        self._stub(monkeypatch, ['partial'], stop_reason='max_tokens')
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))
        done = [p for e, p in frames if e == 'done'][0]
        assert done['truncated'] is True
        assert 'This summary is incomplete' in done['html']

    def test_no_entries_yields_an_error_frame(self, client, admin_user, db, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        self._stub(monkeypatch, ['unused'])
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))
        assert frames == [('error', {'message': (
            'No entries matched. Adjust filters or select rows in the preview first.'
        )})]

    def test_a_failure_mid_generation_becomes_an_error_frame(self, client, admin_user, entry, settings, monkeypatch):
        """The browser must be told, not left with a half stream and no reason."""
        settings.ANTHROPIC_API_KEY = ''      # _prepare raises before any call
        client.force_login(admin_user)
        frames = _sse_frames(client.post(reverse('reports:summary-stream'), {}))
        events = [e for e, _ in frames]
        assert 'error' in events
        assert 'ANTHROPIC_API_KEY' in [p['message'] for e, p in frames if e == 'error'][0]

    def test_stream_uses_the_higher_ceiling(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        settings.ANTHROPIC_MAX_TOKENS = 24000
        settings.ANTHROPIC_STREAM_MAX_TOKENS = 40000
        captured = {}
        stub = _FakeStreamingAnthropic(captured, 'end_turn', ['x'])
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        list(ai_summary.generate_stream(WorkItem.objects.all()))
        assert captured["max_tokens"] == 40000

    def test_stream_falls_back_to_the_blocking_ceiling_when_unset(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        settings.ANTHROPIC_MAX_TOKENS = 24000
        settings.ANTHROPIC_STREAM_MAX_TOKENS = 0
        captured = {}
        stub = _FakeStreamingAnthropic(captured, 'end_turn', ['x'])
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        list(ai_summary.generate_stream(WorkItem.objects.all()))
        assert captured['max_tokens'] == 24000

    def test_generate_stream_ends_with_a_result(self, db, entry, settings, monkeypatch):
        settings.ANTHROPIC_API_KEY = 'k'
        settings.ANTHROPIC_MAX_INPUT_TOKENS = 0
        stub = _FakeStreamingAnthropic({}, 'end_turn', ['a', 'b'])
        monkeypatch.setattr('anthropic.Anthropic', lambda **kw: stub)

        from apps.reports import ai_summary
        events = list(ai_summary.generate_stream(WorkItem.objects.all()))
        assert [k for k, _ in events] == ['delta', 'delta', 'done']
        assert events[-1][1].text == 'ab'

    def test_blocking_endpoint_still_works(self, client, admin_user, entry, monkeypatch):
        """The SSE route is additive — the old endpoint stays for scripts."""
        monkeypatch.setattr(
            'apps.reports.views.ai_summary.generate',
            lambda qs, **kw: _summary_result('## Still here'),
        )
        client.force_login(admin_user)
        resp = client.post(reverse('reports:summary'), {})
        assert resp.status_code == 200
        assert b'Still here' in resp.content
