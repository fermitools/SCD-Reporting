import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.taxonomy.models import Category, EntryType, Project, Tag

User = get_user_model()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username='tax-admin', email='tax-admin@example.com',
        password='pass', role=User.Role.ADMIN,
    )


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(
        username='tax-user', email='tax-user@example.com',
        password='pass', role=User.Role.USER,
    )


# ── Model tests ──────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_project_slug_auto_generated():
    p = Project.objects.create(name='My Project')
    assert p.slug == 'my-project'


@pytest.mark.django_db
def test_project_slug_not_overwritten():
    p = Project.objects.create(name='DUNE', slug='dune-custom')
    assert p.slug == 'dune-custom'


@pytest.mark.django_db
def test_tag_name_lowercased_on_save():
    t = Tag.objects.create(name='  MachineLearning  ')
    assert t.name == 'machinelearning'


@pytest.mark.django_db
def test_tag_unique_constraint():
    Tag.objects.create(name='htmx')
    with pytest.raises(Exception):
        Tag.objects.create(name='htmx')


# ── Access control ────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_projects_page_requires_admin(client, regular_user):
    client.force_login(regular_user)
    assert client.get('/taxonomy/projects/').status_code == 403


@pytest.mark.django_db
def test_projects_page_accessible_to_admin(client, admin_user):
    client.force_login(admin_user)
    assert client.get('/taxonomy/projects/').status_code == 200


@pytest.mark.django_db
def test_categories_page_requires_admin(client, regular_user):
    client.force_login(regular_user)
    assert client.get('/taxonomy/categories/').status_code == 403


@pytest.mark.django_db
def test_tag_autocomplete_requires_login(client):
    assert client.get('/taxonomy/tags/autocomplete/?q=du').status_code == 302


@pytest.mark.django_db
def test_tag_autocomplete_accessible_to_regular_user(client, regular_user):
    client.force_login(regular_user)
    assert client.get('/taxonomy/tags/autocomplete/?q=du').status_code == 200


# ── CRUD ──────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_create_project_via_post(client, admin_user):
    client.force_login(admin_user)
    resp = client.post('/taxonomy/projects/', {'name': 'Test', 'short_code': 'TST',
                                               'is_active': True, 'sort_order': 99})
    assert resp.status_code == 302
    assert Project.objects.filter(name='Test').exists()


@pytest.mark.django_db
def test_entry_types_page_requires_admin(client, regular_user):
    client.force_login(regular_user)
    assert client.get('/taxonomy/entry-types/').status_code == 403


@pytest.mark.django_db
def test_entry_types_page_accessible_to_admin(client, admin_user):
    client.force_login(admin_user)
    assert client.get('/taxonomy/entry-types/').status_code == 200


@pytest.mark.django_db
def test_create_entry_type_via_post(client, admin_user):
    client.force_login(admin_user)
    resp = client.post('/taxonomy/entry-types/', {'name': 'Milestone', 'short_code': 'MS',
                                                  'is_active': True, 'sort_order': 5})
    assert resp.status_code == 302
    assert EntryType.objects.filter(name='Milestone').exists()


@pytest.mark.django_db
def test_edit_project(client, admin_user):
    p = Project.objects.create(name='OldName')
    client.force_login(admin_user)
    resp = client.post(f'/taxonomy/projects/{p.pk}/edit/',
                       {'name': 'NewName', 'short_code': '', 'is_active': True, 'sort_order': 0})
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.name == 'NewName'


# ── seed_taxonomy ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_seed_taxonomy_idempotent():
    from django.core.management import call_command
    call_command('seed_taxonomy', verbosity=0)
    call_command('seed_taxonomy', verbosity=0)
    assert Project.objects.filter(name='DUNE').count() == 1
    assert Category.objects.filter(name='Scientific').count() == 1


@pytest.mark.django_db
def test_seed_taxonomy_creates_expected_data():
    from django.core.management import call_command
    call_command('seed_taxonomy', verbosity=0)
    assert Project.objects.count() == 5
    assert Category.objects.count() == 4
    assert EntryType.objects.count() == 3
    assert Project.objects.filter(name='MicroBooNE').exists()
    assert Category.objects.filter(name='Outreach').exists()
    assert EntryType.objects.filter(name='Weekly Report').exists()


# ── Tag autocomplete content ──────────────────────────────────────────────────

@pytest.mark.django_db
def test_tag_autocomplete_returns_matching_tags(client, regular_user):
    Tag.objects.create(name='django')
    Tag.objects.create(name='htmx')
    client.force_login(regular_user)
    resp = client.get('/taxonomy/tags/autocomplete/?q=dj')
    assert b'django' in resp.content
    assert b'htmx' not in resp.content


@pytest.mark.django_db
def test_tag_autocomplete_empty_query_returns_empty(client, regular_user):
    client.force_login(regular_user)
    resp = client.get('/taxonomy/tags/autocomplete/?q=')
    assert resp.status_code == 200
    assert resp.content == b''


# ── Taxonomy import (issue #11) ──────────────────────────────────────────────

def _upload(payload, name='taxonomy.json'):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return SimpleUploadedFile(name, raw, content_type='application/json')


def _post_import(client, payload):
    resp = client.post(reverse('taxonomy:import'), {'taxonomy_file': _upload(payload)})
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    return resp, msgs


@pytest.mark.django_db
def test_import_requires_admin(client, regular_user):
    client.force_login(regular_user)
    resp = client.post(reverse('taxonomy:import'), {'taxonomy_file': _upload({'projects': []})})
    assert resp.status_code in (302, 403)
    assert Project.objects.count() == 0


@pytest.mark.django_db
def test_import_creates_and_updates(client, admin_user):
    Project.objects.create(name='DUNE', slug='dune', sort_order=5)
    client.force_login(admin_user)
    resp, msgs = _post_import(client, {
        'projects': [
            {'name': 'DUNE', 'slug': 'dune', 'sort_order': 1},
            {'name': 'NOvA', 'slug': 'nova', 'short_code': 'NV'},
        ],
        'categories': [{'name': 'Ops'}],
    })
    assert resp.status_code == 302
    assert any('projects: 1 created, 1 updated' in m for m in msgs), msgs
    assert Project.objects.get(slug='dune').sort_order == 1
    assert Project.objects.get(slug='nova').short_code == 'NV'
    assert Category.objects.filter(name='Ops').exists()


@pytest.mark.django_db
def test_import_conflict_rolls_back_everything(client, admin_user):
    """A unique-name collision must abort the whole import, not half of it."""
    Project.objects.create(name='DUNE', slug='dune')
    client.force_login(admin_user)
    resp, msgs = _post_import(client, {
        'projects': [
            {'name': 'Zeta', 'slug': 'zeta'},          # valid, would be created first
            {'name': 'DUNE', 'slug': 'dune-copy'},     # name collides with existing row
        ],
    })
    assert resp.status_code == 302
    assert any('no changes were made' in m and 'dune-copy' in m for m in msgs), msgs
    assert not Project.objects.filter(slug='zeta').exists()
    assert not Project.objects.filter(slug='dune-copy').exists()
    assert Project.objects.count() == 1


@pytest.mark.django_db
def test_import_rejects_malformed_records(client, admin_user):
    client.force_login(admin_user)
    resp, msgs = _post_import(client, {'projects': [{'name': 'Ok'}, 'not-an-object']})
    assert any('no changes were made' in m for m in msgs), msgs
    assert Project.objects.count() == 0

    resp, msgs = _post_import(client, {'projects': {'name': 'not-a-list'}})
    assert any('must be a list' in m for m in msgs), msgs


@pytest.mark.django_db
def test_import_rejects_invalid_json(client, admin_user):
    client.force_login(admin_user)
    resp, msgs = _post_import(client, b'{not json')
    assert resp.status_code == 302
    assert any('Invalid JSON' in m for m in msgs), msgs


@pytest.mark.django_db
def test_import_rejects_oversized_file(client, admin_user, settings):
    settings.TAXONOMY_IMPORT_MAX_BYTES = 64
    client.force_login(admin_user)
    big = {'projects': [{'name': f'P{i}'} for i in range(50)]}
    resp, msgs = _post_import(client, big)
    assert resp.status_code == 302
    assert any('too large' in m for m in msgs), msgs
    assert Project.objects.count() == 0
