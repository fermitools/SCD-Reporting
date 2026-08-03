"""Tests for the entries app: WorkItem CRUD, period prefill, markdown preview."""
import pytest
from datetime import date, timedelta

from django.urls import reverse

from apps.accounts.models import User
from apps.entries.models import WorkItem
from apps.taxonomy.models import Category, Project, Tag


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
def user(db):
    return User.objects.create_user(username='tester', email='tester@example.com', password='pass')


@pytest.fixture
def project(db):
    return Project.objects.create(name='TestProject', slug='testproject')


@pytest.fixture
def category(db):
    return Category.objects.create(name='TestCat', slug='testcat')


@pytest.fixture
def entry(db, user, project, category):
    today = date.today()
    return _new_entry(
        author=user,
        title='My entry',
        project=project,
        category=category,
        period_kind='week',
        period_start=today - timedelta(days=today.weekday()),
        period_end=today - timedelta(days=today.weekday()) + timedelta(days=6),
        description='Hello **world**',
    )


# ── List ──────────────────────────────────────────────────────────────────────

class TestEntryList:
    def test_redirects_anonymous(self, client):
        resp = client.get(reverse('entries:list'))
        assert resp.status_code == 302

    def test_shows_own_entries(self, client, user, entry):
        client.force_login(user)
        resp = client.get(reverse('entries:list'))
        assert resp.status_code == 200
        assert entry.title in resp.content.decode()

    def test_hides_other_users_entries(self, db, client, entry, project, category):
        other = User.objects.create_user(username='other', email='other@example.com', password='pass')
        client.force_login(other)
        resp = client.get(reverse('entries:list'))
        assert entry.title not in resp.content.decode()

    def _make_entry(self, user, project, category, **kwargs):
        today = date.today()
        defaults = dict(
            author=user, project=project, category=category, period_kind='week',
            period_start=today, period_end=today, description='',
        )
        defaults.update(kwargs)
        return _new_entry(**defaults)

    def test_search_filters_by_title(self, db, client, user, project, category):
        self._make_entry(user, project, category, title='Quarterly budget review')
        self._make_entry(user, project, category, title='Server migration')
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'budget'})
        body = resp.content.decode()
        assert 'Quarterly budget review' in body
        assert 'Server migration' not in body

    def test_search_filters_by_description(self, db, client, user, project, category):
        self._make_entry(user, project, category, title='Alpha', description='notes about kubernetes')
        self._make_entry(user, project, category, title='Beta', description='unrelated text')
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'kubernetes'})
        body = resp.content.decode()
        assert 'Alpha' in body
        assert 'Beta' not in body

    def test_search_filters_by_tag(self, db, client, user, project, category):
        tagged = self._make_entry(user, project, category, title='Tagged item')
        tagged.tags.add(Tag.objects.create(name='networking'))
        self._make_entry(user, project, category, title='Untagged item')
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'network'})
        body = resp.content.decode()
        assert 'Tagged item' in body
        assert 'Untagged item' not in body

    def test_search_only_returns_own_entries(self, db, client, user, project, category):
        other = User.objects.create_user(username='other2', email='other2@example.com', password='pass')
        self._make_entry(other, project, category, title='Shared keyword here')
        mine = self._make_entry(user, project, category, title='Shared keyword mine')
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'Shared keyword'})
        body = resp.content.decode()
        assert 'Shared keyword mine' in body
        assert 'Shared keyword here' not in body

    def test_search_no_match_shows_empty_state(self, db, client, user, project, category):
        self._make_entry(user, project, category, title='Real entry')
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'zzz-no-match'})
        assert 'No entries match your search.' in resp.content.decode()

    def test_search_does_not_duplicate_multi_tag_matches(self, db, client, user, project, category):
        item = self._make_entry(user, project, category, title='Multi tag entry')
        item.tags.add(Tag.objects.create(name='netops'))
        item.tags.add(Tag.objects.create(name='network-core'))
        client.force_login(user)
        resp = client.get(reverse('entries:list'), {'q': 'net'})
        # Title should appear once in the table despite matching two tags.
        assert resp.content.decode().count('>Multi tag entry<') == 1


# ── Create ────────────────────────────────────────────────────────────────────

class TestEntryCreate:
    def test_get_renders_form(self, client, user):
        client.force_login(user)
        resp = client.get(reverse('entries:create'))
        assert resp.status_code == 200
        assert b'period_start' in resp.content

    def test_post_creates_entry(self, client, user, project, category):
        client.force_login(user)
        today = date.today()
        resp = client.post(reverse('entries:create'), {
            'title': 'New entry',
            'projects': [project.pk],
            'categories': [category.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'Test description',
            'tags_input': '',
        })
        assert resp.status_code == 302
        assert WorkItem.objects.filter(title='New entry', author=user).exists()

    def test_post_creates_entry_with_multiple_projects(self, client, user, project, category):
        p2 = Project.objects.create(name='Second', slug='second')
        c2 = Category.objects.create(name='SecondCat', slug='secondcat')
        client.force_login(user)
        today = date.today()
        client.post(reverse('entries:create'), {
            'title': 'Multi entry',
            'projects': [project.pk, p2.pk],
            'categories': [category.pk, c2.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': '',
        })
        entry = WorkItem.objects.get(title='Multi entry')
        assert set(entry.projects.values_list('pk', flat=True)) == {project.pk, p2.pk}
        assert set(entry.categories.values_list('pk', flat=True)) == {category.pk, c2.pk}

    def test_post_requires_at_least_one_project(self, client, user, project, category):
        client.force_login(user)
        today = date.today()
        resp = client.post(reverse('entries:create'), {
            'title': 'No project',
            'categories': [category.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': '',
        })
        assert resp.status_code == 200  # form re-rendered with validation errors
        assert not WorkItem.objects.filter(title='No project').exists()

    def test_sets_author_to_current_user(self, client, user, project, category):
        client.force_login(user)
        today = date.today()
        client.post(reverse('entries:create'), {
            'title': 'Authored',
            'projects': [project.pk],
            'categories': [category.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': '',
        })
        assert WorkItem.objects.get(title='Authored').author == user

    def test_post_with_entry_type_sets_it(self, client, user, project, category):
        from apps.taxonomy.models import EntryType
        et = EntryType.objects.create(name='Weekly Report', slug='weekly-report')
        client.force_login(user)
        today = date.today()
        client.post(reverse('entries:create'), {
            'title': 'Typed entry',
            'projects': [project.pk],
            'categories': [category.pk],
            'entry_type': et.pk,
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': '',
        })
        assert WorkItem.objects.get(title='Typed entry').entry_type == et

    def test_entry_type_is_optional(self, client, user, project, category):
        client.force_login(user)
        today = date.today()
        resp = client.post(reverse('entries:create'), {
            'title': 'No type',
            'projects': [project.pk],
            'categories': [category.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': '',
        })
        assert resp.status_code == 302
        assert WorkItem.objects.get(title='No type').entry_type is None

    def test_post_with_tags_increments_use_count(self, client, user, project, category):
        tag = Tag.objects.create(name='mytag')
        client.force_login(user)
        today = date.today()
        client.post(reverse('entries:create'), {
            'title': 'Tagged',
            'projects': [project.pk],
            'categories': [category.pk],
            'period_kind': 'week',
            'period_start': (today - timedelta(days=today.weekday())).isoformat(),
            'period_end':   (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat(),
            'description': 'desc',
            'tags_input': 'mytag',
        })
        tag.refresh_from_db()
        assert tag.use_count == 1


# ── Detail ────────────────────────────────────────────────────────────────────

class TestEntryDetail:
    def test_renders_markdown(self, client, user, entry):
        client.force_login(user)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200
        assert b'<strong>world</strong>' in resp.content

    def test_sanitizes_unsafe_markdown_html(self, client, user, entry):
        entry.description = 'Safe **bold** <img src=x onerror=alert(1)> [bad](javascript:alert(1))'
        entry.save()
        client.force_login(user)

        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))

        assert resp.status_code == 200
        assert b'<strong>bold</strong>' in resp.content
        assert b'<img' not in resp.content
        assert b'onerror' not in resp.content
        assert b'javascript:' not in resp.content

    def test_other_user_gets_404(self, db, client, entry):
        other = User.objects.create_user(username='other2', email='other2@example.com', password='pass')
        client.force_login(other)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 404

    def test_group_leader_can_view_others_entry(self, db, client, entry):
        leader = User.objects.create_user(username='leader', email='leader@example.com', password='pass',
                                          role=User.Role.GROUP_LEADER)
        client.force_login(leader)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200

    def test_functional_lead_can_view_others_entry(self, db, client, entry):
        lead = User.objects.create_user(username='flead', email='flead@example.com', password='pass',
                                        role=User.Role.FUNCTIONAL_LEAD)
        client.force_login(lead)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200

    def test_shows_author_display_name(self, client, user, entry):
        user.display_name = 'Alice Tester'
        user.save(update_fields=['display_name'])
        client.force_login(user)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200
        assert b'Alice Tester' in resp.content

    def test_shows_author_email_when_no_display_name(self, client, user, entry):
        user.display_name = ''
        user.save(update_fields=['display_name'])
        client.force_login(user)
        resp = client.get(reverse('entries:detail', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200
        assert b'tester@example.com' in resp.content


# ── Edit ──────────────────────────────────────────────────────────────────────

class TestEntryEdit:
    def test_can_edit_own_entry(self, client, user, entry):
        client.force_login(user)
        resp = client.post(reverse('entries:edit', kwargs={'pk': entry.pk}), {
            'title': 'Updated title',
            'projects': list(entry.projects.values_list('pk', flat=True)),
            'categories': list(entry.categories.values_list('pk', flat=True)),
            'period_kind': entry.period_kind,
            'period_start': entry.period_start.isoformat(),
            'period_end':   entry.period_end.isoformat(),
            'description': entry.description,
            'tags_input': '',
        })
        assert resp.status_code == 302
        entry.refresh_from_db()
        assert entry.title == 'Updated title'

    def test_edit_form_shows_existing_dates(self, client, user, entry):
        client.force_login(user)
        resp = client.get(reverse('entries:edit', kwargs={'pk': entry.pk}))
        assert resp.status_code == 200
        assert entry.period_start.isoformat().encode() in resp.content
        assert entry.period_end.isoformat().encode() in resp.content

    def test_cannot_edit_other_users_entry(self, db, client, entry):
        other = User.objects.create_user(username='other3', email='other3@example.com', password='pass')
        client.force_login(other)
        resp = client.post(reverse('entries:edit', kwargs={'pk': entry.pk}), {
            'title': 'Hijacked',
            'projects': list(entry.projects.values_list('pk', flat=True)),
            'categories': list(entry.categories.values_list('pk', flat=True)),
            'period_kind': entry.period_kind,
            'period_start': entry.period_start.isoformat(),
            'period_end':   entry.period_end.isoformat(),
            'description': '',
            'tags_input': '',
        })
        assert resp.status_code == 404
        entry.refresh_from_db()
        assert entry.title != 'Hijacked'


# ── Delete ────────────────────────────────────────────────────────────────────

class TestEntryDelete:
    def test_delete_decrements_tag_use_count(self, client, user, project, category):
        tag = Tag.objects.create(name='deletetag', use_count=1)
        today = date.today()
        item = _new_entry(
            author=user, title='To delete', project=project, category=category,
            period_kind='week',
            period_start=today - timedelta(days=today.weekday()),
            period_end=today - timedelta(days=today.weekday()) + timedelta(days=6),
            description='',
        )
        item.tags.add(tag)
        client.force_login(user)
        client.post(reverse('entries:delete', kwargs={'pk': item.pk}))
        tag.refresh_from_db()
        assert tag.use_count == 0
        assert not WorkItem.objects.filter(pk=item.pk).exists()


# ── HTMX: period prefill ──────────────────────────────────────────────────────

class TestPeriodPrefill:
    @pytest.mark.parametrize('kind', ['week', 'fortnight', 'month', 'custom'])
    def test_returns_date_inputs(self, client, user, kind):
        client.force_login(user)
        resp = client.get(reverse('entries:period-prefill') + f'?kind={kind}')
        assert resp.status_code == 200
        assert b'period_start' in resp.content
        assert b'period_end' in resp.content

    def test_week_start_is_monday(self, client, user):
        client.force_login(user)
        resp = client.get(reverse('entries:period-prefill') + '?kind=week')
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        assert monday.isoformat().encode() in resp.content


# ── HTMX: markdown preview ────────────────────────────────────────────────────

class TestMarkdownPreview:
    def test_renders_html(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {'description': '**bold**'})
        assert resp.status_code == 200
        assert b'<strong>bold</strong>' in resp.content

    def test_sanitizes_unsafe_html(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {
            'description': '<script>alert(1)</script><img src=x onerror=alert(1)> **ok**',
        })
        assert resp.status_code == 200
        assert b'<strong>ok</strong>' in resp.content
        assert b'<script' not in resp.content
        assert b'<img' not in resp.content
        assert b'onerror' not in resp.content

    def test_empty_description_returns_empty(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {'description': ''})
        assert resp.status_code == 200

    def test_autolinks_bare_url(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {
            'description': 'See https://cds.cern.ch/record/2967685 for details.',
        })
        assert resp.status_code == 200
        assert (
            b'<a href="https://cds.cern.ch/record/2967685">'
            b'https://cds.cern.ch/record/2967685</a>' in resp.content
        )

    def test_autolink_does_not_swallow_trailing_punctuation(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {
            'description': 'Visit https://example.com, then https://example.com/other.',
        })
        assert resp.status_code == 200
        assert b'<a href="https://example.com">https://example.com</a>,' in resp.content
        assert b'<a href="https://example.com/other">https://example.com/other</a>.' in resp.content

    def test_autolink_does_not_duplicate_existing_markdown_link(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:markdown-preview'), {
            'description': '[the paper](https://example.com/already)',
        })
        assert resp.status_code == 200
        assert resp.content.count(b'<a ') == 1
        assert b'<a href="https://example.com/already">the paper</a>' in resp.content


# ── Entry description templates ───────────────────────────────────────────────

class TestEntryTemplates:
    def test_create_form_includes_templates_in_context(self, client, user):
        from apps.entries.models import EntryTemplate
        EntryTemplate.objects.create(user=user, name='Weekly', body='## Weekly\n')
        client.force_login(user)
        resp = client.get(reverse('entries:create'))
        assert resp.status_code == 200
        assert b'Weekly' in resp.content

    def test_save_as_creates_new_template(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('entries:template-save'), {
            'name': 'My Snippet', 'body': 'Hello world',
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['name'] == 'My Snippet'
        from apps.entries.models import EntryTemplate
        assert EntryTemplate.objects.filter(user=user, name='My Snippet').exists()

    def test_save_updates_existing_template(self, client, user):
        from apps.entries.models import EntryTemplate
        tpl = EntryTemplate.objects.create(user=user, name='Old', body='old body')
        client.force_login(user)
        resp = client.post(reverse('entries:template-save'), {
            'pk': tpl.pk, 'body': 'new body',
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        tpl.refresh_from_db()
        assert tpl.body == 'new body'

    def test_save_wrong_user_returns_400(self, client, user, db):
        other = User.objects.create_user(username='other9', email='other9@example.com', password='pass')
        from apps.entries.models import EntryTemplate
        tpl = EntryTemplate.objects.create(user=other, name='Theirs', body='x')
        client.force_login(user)
        resp = client.post(reverse('entries:template-save'), {
            'pk': tpl.pk, 'body': 'hijack',
        })
        assert resp.status_code == 400

    def test_delete_own_template(self, client, user):
        from apps.entries.models import EntryTemplate
        tpl = EntryTemplate.objects.create(user=user, name='ToDelete', body='x')
        client.force_login(user)
        resp = client.post(reverse('entries:template-delete'), {'pk': tpl.pk})
        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert not EntryTemplate.objects.filter(pk=tpl.pk).exists()

    def test_delete_wrong_user_returns_404(self, client, user, db):
        other = User.objects.create_user(username='other10', email='other10@example.com', password='pass')
        from apps.entries.models import EntryTemplate
        tpl = EntryTemplate.objects.create(user=other, name='Theirs', body='x')
        client.force_login(user)
        resp = client.post(reverse('entries:template-delete'), {'pk': tpl.pk})
        assert resp.status_code == 404

    def test_save_as_idempotent_on_same_name(self, client, user):
        from apps.entries.models import EntryTemplate
        tpl = EntryTemplate.objects.create(user=user, name='Dup', body='v1')
        client.force_login(user)
        resp = client.post(reverse('entries:template-save'), {
            'name': 'Dup', 'body': 'v2',
        })
        assert resp.status_code == 200
        tpl.refresh_from_db()
        assert tpl.body == 'v2'
        assert EntryTemplate.objects.filter(user=user, name='Dup').count() == 1


# ── Entry management (admin / division head / group leader) ───────────────────

class TestEntryManagement:
    """
    Covers EntryManageView, EntryReassignView, EntryArchiveView, and
    EntryManagerDeleteView (Issue #12 — missing test coverage).

    Fixtures:
        author      — a regular user who owns the entry
        entry       — a WorkItem belonging to author
        group_leader — a GROUP_LEADER
        div_head    — a DIVISION_HEAD
        admin       — an ADMIN
    """

    # ── shared fixtures ───────────────────────────────────────────────────────

    @pytest.fixture
    def author(self, db):
        return User.objects.create_user(
            username='author_mgmt', email='author_mgmt@example.com', password='pass',
        )

    @pytest.fixture
    def managed_entry(self, db, author, project, category):
        today = date.today()
        start = today - timedelta(days=today.weekday())
        return _new_entry(
            author=author,
            title='Manager target entry',
            project=project,
            category=category,
            period_kind='week',
            period_start=start,
            period_end=start + timedelta(days=6),
            description='manageable',
        )

    @pytest.fixture
    def group_leader(self, db):
        return User.objects.create_user(
            username='gl_mgmt', email='gl_mgmt@example.com', password='pass',
            role=User.Role.GROUP_LEADER,
        )

    @pytest.fixture
    def div_head(self, db):
        return User.objects.create_user(
            username='dh_mgmt', email='dh_mgmt@example.com', password='pass',
            role=User.Role.DIVISION_HEAD,
        )

    @pytest.fixture
    def admin(self, db):
        return User.objects.create_user(
            username='admin_mgmt', email='admin_mgmt@example.com', password='pass',
            role=User.Role.ADMIN,
        )

    # ── EntryManageView (/entries/manage/) ────────────────────────────────────

    def test_manage_anonymous_redirects(self, client):
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 302
        assert '/accounts/login/' in resp['Location']

    def test_manage_regular_user_forbidden(self, client, user):
        client.force_login(user)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 403

    def test_manage_group_leader_allowed(self, client, group_leader):
        client.force_login(group_leader)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200

    def test_manage_division_head_allowed(self, client, div_head):
        client.force_login(div_head)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200

    def test_manage_admin_allowed(self, client, admin):
        client.force_login(admin)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200

    def test_manage_lists_entries(self, client, admin, managed_entry):
        client.force_login(admin)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200
        assert managed_entry.title.encode() in resp.content

    def test_manage_shows_archived_when_requested(self, client, admin, managed_entry):
        managed_entry.is_archived = True
        managed_entry.save(update_fields=['is_archived', 'updated_at'])
        client.force_login(admin)
        resp = client.get(reverse('entries:manage') + '?archived=1')
        assert resp.status_code == 200
        assert managed_entry.title.encode() in resp.content

    def test_manage_hides_archived_by_default(self, client, admin, managed_entry):
        managed_entry.is_archived = True
        managed_entry.save(update_fields=['is_archived', 'updated_at'])
        client.force_login(admin)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200
        assert managed_entry.title.encode() not in resp.content

    def test_manage_hides_division_head_only_from_group_leader(
        self, client, group_leader, managed_entry
    ):
        managed_entry.is_division_head_only = True
        managed_entry.save(update_fields=['is_division_head_only', 'updated_at'])
        client.force_login(group_leader)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200
        assert managed_entry.title.encode() not in resp.content

    def test_manage_shows_division_head_only_to_admin(self, client, admin, managed_entry):
        managed_entry.is_division_head_only = True
        managed_entry.save(update_fields=['is_division_head_only', 'updated_at'])
        client.force_login(admin)
        resp = client.get(reverse('entries:manage'))
        assert resp.status_code == 200
        assert managed_entry.title.encode() in resp.content

    def test_manage_title_search_filter(self, client, admin, managed_entry, project, category, author):
        today = date.today()
        start = today - timedelta(days=today.weekday())
        other = _new_entry(
            author=author, title='Completely different', project=project,
            category=category, period_kind='week',
            period_start=start, period_end=start + timedelta(days=6),
            description='',
        )
        client.force_login(admin)
        resp = client.get(reverse('entries:manage') + '?q=Manager+target')
        assert resp.status_code == 200
        assert managed_entry.title.encode() in resp.content
        assert other.title.encode() not in resp.content

    # ── EntryArchiveView (/entries/<pk>/archive/) ─────────────────────────────

    def test_archive_anonymous_redirects(self, client, managed_entry):
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        assert '/accounts/login/' in resp['Location']

    def test_archive_regular_user_forbidden(self, client, user, managed_entry):
        client.force_login(user)
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 403

    def test_archive_sets_is_archived_true(self, client, admin, managed_entry):
        client.force_login(admin)
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.is_archived is True

    def test_unarchive_sets_is_archived_false(self, client, admin, managed_entry):
        managed_entry.is_archived = True
        managed_entry.save(update_fields=['is_archived', 'updated_at'])
        client.force_login(admin)
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.is_archived is False

    def test_archive_group_leader_can_archive(self, client, group_leader, managed_entry):
        client.force_login(group_leader)
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.is_archived is True

    def test_archive_division_head_can_archive(self, client, div_head, managed_entry):
        client.force_login(div_head)
        resp = client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.is_archived is True

    def test_archive_creates_audit_log(self, client, admin, managed_entry):
        from apps.audit.models import AuditLogEntry
        AuditLogEntry.objects.all().delete()
        client.force_login(admin)
        client.post(reverse('entries:archive', kwargs={'pk': managed_entry.pk}))
        entry = AuditLogEntry.objects.filter(action='update', object_id=managed_entry.pk).first()
        assert entry is not None
        assert 'is_archived' in entry.changes

    # ── EntryReassignView (/entries/<pk>/reassign/) ───────────────────────────

    def test_reassign_anonymous_redirects(self, client, managed_entry, user):
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        assert resp.status_code == 302
        assert '/accounts/login/' in resp['Location']

    def test_reassign_regular_user_forbidden(self, client, user, managed_entry):
        client.force_login(user)
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        assert resp.status_code == 403

    def test_reassign_changes_author(self, client, admin, managed_entry, user):
        client.force_login(admin)
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.author == user

    def test_reassign_group_leader_can_reassign(self, client, group_leader, managed_entry, user):
        client.force_login(group_leader)
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.author == user

    def test_reassign_division_head_can_reassign(self, client, div_head, managed_entry, user):
        client.force_login(div_head)
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        assert managed_entry.author == user

    def test_reassign_invalid_author_id_redirects(self, client, admin, managed_entry):
        client.force_login(admin)
        resp = client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': 999999},
        )
        assert resp.status_code == 302
        managed_entry.refresh_from_db()
        # author should be unchanged
        assert managed_entry.author.email == 'author_mgmt@example.com'

    def test_reassign_creates_audit_log(self, client, admin, managed_entry, user):
        from apps.audit.models import AuditLogEntry
        AuditLogEntry.objects.all().delete()
        client.force_login(admin)
        client.post(
            reverse('entries:reassign', kwargs={'pk': managed_entry.pk}),
            {'author_id': user.pk},
        )
        entry = AuditLogEntry.objects.filter(action='update', object_id=managed_entry.pk).first()
        assert entry is not None
        assert 'author_id' in entry.changes

    # ── EntryManagerDeleteView (/entries/<pk>/manager-delete/) ────────────────

    def test_manager_delete_anonymous_redirects(self, client, managed_entry):
        resp = client.post(reverse('entries:manager-delete', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 302
        assert '/accounts/login/' in resp['Location']

    def test_manager_delete_regular_user_forbidden(self, client, user, managed_entry):
        client.force_login(user)
        resp = client.post(reverse('entries:manager-delete', kwargs={'pk': managed_entry.pk}))
        assert resp.status_code == 403
        assert WorkItem.objects.filter(pk=managed_entry.pk).exists()

    def test_manager_delete_admin_removes_entry(self, client, admin, managed_entry):
        pk = managed_entry.pk
        client.force_login(admin)
        resp = client.post(reverse('entries:manager-delete', kwargs={'pk': pk}))
        assert resp.status_code == 302
        assert not WorkItem.objects.filter(pk=pk).exists()

    def test_manager_delete_group_leader_removes_entry(self, client, group_leader, managed_entry):
        pk = managed_entry.pk
        client.force_login(group_leader)
        resp = client.post(reverse('entries:manager-delete', kwargs={'pk': pk}))
        assert resp.status_code == 302
        assert not WorkItem.objects.filter(pk=pk).exists()

    def test_manager_delete_division_head_removes_entry(self, client, div_head, managed_entry):
        pk = managed_entry.pk
        client.force_login(div_head)
        resp = client.post(reverse('entries:manager-delete', kwargs={'pk': pk}))
        assert resp.status_code == 302
        assert not WorkItem.objects.filter(pk=pk).exists()

    def test_manager_delete_decrements_tag_use_count(self, client, admin, managed_entry):
        tag = Tag.objects.create(name='mgr_delete_tag', use_count=2)
        managed_entry.tags.add(tag)
        pk = managed_entry.pk
        client.force_login(admin)
        client.post(reverse('entries:manager-delete', kwargs={'pk': pk}))
        tag.refresh_from_db()
        assert tag.use_count == 1
        assert not WorkItem.objects.filter(pk=pk).exists()

    def test_manager_delete_creates_audit_log(self, client, admin, managed_entry):
        from apps.audit.models import AuditLogEntry
        AuditLogEntry.objects.all().delete()
        pk = managed_entry.pk
        client.force_login(admin)
        client.post(reverse('entries:manager-delete', kwargs={'pk': pk}))
        # post_delete signal fires → audit 'delete' entry is recorded
        entry = AuditLogEntry.objects.filter(action='delete', object_id=pk).first()
        assert entry is not None
