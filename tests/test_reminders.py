"""Tests for the email reminder subsystem (GitHub #31).

Covers scope resolution per role, placeholder substitution, the dedupe window,
the staleness filter, next-fire arithmetic (including DST), the atomic claim
that makes multi-worker firing safe, and permission gating on every view.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.entries.models import WorkItem
from apps.reminders import scheduling, scope, service
from apps.reminders.models import ReminderLog, ReminderSchedule, ReminderTemplate
from apps.taxonomy.models import Project, WorkGroup


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def group_a(db):
    return WorkGroup.objects.create(name='Group A', slug='group-a')


@pytest.fixture
def group_b(db):
    return WorkGroup.objects.create(name='Group B', slug='group-b')


@pytest.fixture
def group_lead(db):
    """Home group for the leadership accounts.

    Every authenticated user needs a primary group — GroupSelectionMiddleware
    redirects those without one — and keeping the leads out of the groups they
    manage makes the scope assertions unambiguous.
    """
    return WorkGroup.objects.create(name='Leadership', slug='leadership')


@pytest.fixture
def project(db):
    return Project.objects.create(name='DUNE', slug='dune')


@pytest.fixture
def other_project(db):
    return Project.objects.create(name='Mu2e', slug='mu2e')


def _user(username, role=User.Role.USER, group=None, email=None):
    return User.objects.create_user(
        username=username,
        email=email or f'{username}@example.com',
        password='pass',
        role=role,
        group=group,
    )


@pytest.fixture
def admin_user(db, group_lead):
    return _user('admin', User.Role.ADMIN, group=group_lead)


@pytest.fixture
def member_a(db, group_a):
    return _user('member_a', group=group_a)


@pytest.fixture
def member_b(db, group_b):
    return _user('member_b', group=group_b)


@pytest.fixture
def group_leader(db, group_a):
    return _user('leader', User.Role.GROUP_LEADER, group=group_a)


@pytest.fixture
def division_head(db, group_a, group_lead):
    head = _user('head', User.Role.DIVISION_HEAD, group=group_lead)
    head.managed_groups.set([group_a])
    return head


@pytest.fixture
def functional_lead(db, project, group_lead):
    lead = _user('flead', User.Role.FUNCTIONAL_LEAD, group=group_lead)
    lead.managed_projects.set([project])
    return lead


@pytest.fixture
def auditor(db, group_lead):
    return _user('auditor', User.Role.AUDITOR, group=group_lead)


@pytest.fixture
def template(db):
    return ReminderTemplate.objects.create(
        name='Test body',
        subject='Reminder for {{recipient_name}}',
        body='Last entry: {{last_entry_summary}}\n\nGo to {{new_entry_url}}\n\n{{nope}}',
        is_default=True,
    )


def _entry(author, days_ago=1, title='Some work', project=None):
    end = timezone.localdate() - timedelta(days=days_ago)
    item = WorkItem.objects.create(
        author=author, title=title,
        description='body',
        period_start=end - timedelta(days=6), period_end=end,
    )
    if project is not None:
        item.projects.set([project])
    return item


# ── scope ─────────────────────────────────────────────────────────────────────

def test_regular_user_cannot_send(db, member_a):
    assert scope.can_send(member_a) is False
    assert list(scope.recipient_qs(member_a)) == []


def test_auditor_cannot_send(db, auditor):
    """Auditor is read-only; sending mail on the division's behalf is a write."""
    assert scope.can_send(auditor) is False


def test_group_leader_scope_is_own_group(db, group_leader, member_a, member_b):
    emails = set(scope.recipient_qs(group_leader).values_list('email', flat=True))
    assert member_a.email in emails
    assert member_b.email not in emails


def test_division_head_scope_is_managed_groups(db, division_head, member_a, member_b):
    emails = set(scope.recipient_qs(division_head).values_list('email', flat=True))
    assert member_a.email in emails
    assert member_b.email not in emails


def test_functional_lead_scope_is_project_authors(
    db, functional_lead, project, other_project, member_a, member_b
):
    _entry(member_a, project=project)
    _entry(member_b, project=other_project)
    emails = set(scope.recipient_qs(functional_lead).values_list('email', flat=True))
    assert member_a.email in emails
    assert member_b.email not in emails


def test_functional_lead_with_no_projects_has_empty_scope(db):
    lead = _user('lonely', User.Role.FUNCTIONAL_LEAD)
    assert list(scope.recipient_qs(lead)) == []


def test_admin_scope_is_everyone_active(db, admin_user, member_a, member_b):
    emails = set(scope.recipient_qs(admin_user).values_list('email', flat=True))
    assert {member_a.email, member_b.email} <= emails


def test_inactive_and_emailless_users_are_out_of_scope(db, admin_user, group_a):
    User.objects.create_user(username='ghost', email='ghost@example.com',
                             password='p', is_active=False)
    User.objects.create_user(username='noemail', email='', password='p')
    emails = set(scope.recipient_qs(admin_user).values_list('username', flat=True))
    assert 'ghost' not in emails
    assert 'noemail' not in emails


# ── rendering ─────────────────────────────────────────────────────────────────

def test_placeholders_substituted_and_unknown_tokens_kept(db, member_a, template, settings):
    settings.REMINDER_BASE_URL = 'https://example.test'
    _entry(member_a, days_ago=3, title='Fixed the DAQ')

    subject, text, html = service.render_reminder(template, member_a)

    assert subject == f'Reminder for {member_a.email}'
    assert 'Fixed the DAQ' in text
    assert 'https://example.test/entries/new/' in text
    # An unrecognised token stays verbatim so the typo is visible.
    assert '{{nope}}' in text
    assert '<a href="https://example.test/entries/new/"' in html


def test_placeholders_for_user_with_no_entries(db, member_a, template):
    _subject, text, _html = service.render_reminder(template, member_a)
    assert 'no entries on record yet' in text


def test_subject_is_forced_to_one_line(db, member_a):
    tpl = ReminderTemplate.objects.create(
        name='multi', subject='Line one\nBcc: attacker@example.com', body='x',
    )
    subject, _text, _html = service.render_reminder(tpl, member_a)
    assert '\n' not in subject


def test_html_alternative_is_sanitised(db, member_a):
    tpl = ReminderTemplate.objects.create(
        name='xss', subject='s', body='<script>alert(1)</script>ok',
    )
    _subject, _text, html = service.render_reminder(tpl, member_a)
    assert '<script>' not in html


# ── sending ───────────────────────────────────────────────────────────────────

def test_send_delivers_multipart_and_logs(db, group_leader, member_a, template):
    mail.outbox.clear()
    result = service.send_reminders([member_a], template=template, sent_by=group_leader)

    assert result['sent'] == 1
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [member_a.email]
    assert message.reply_to == [group_leader.email]
    assert message.alternatives[0][1] == 'text/html'

    log = ReminderLog.objects.get()
    assert log.status == ReminderLog.Status.SENT
    assert log.recipient == member_a
    assert log.sent_by == group_leader


def test_dedupe_window_skips_a_second_send(db, group_leader, member_a, template, settings):
    settings.REMINDER_MIN_INTERVAL_HOURS = 20
    mail.outbox.clear()
    service.send_reminders([member_a], template=template, sent_by=group_leader)
    second = service.send_reminders([member_a], template=template, sent_by=group_leader)

    assert second['sent'] == 0
    assert second['skipped'] == 1
    assert len(mail.outbox) == 1
    assert ReminderLog.objects.filter(status=ReminderLog.Status.SKIPPED).count() == 1


def test_ignore_interval_overrides_the_dedupe_window(db, group_leader, member_a, template, settings):
    settings.REMINDER_MIN_INTERVAL_HOURS = 20
    mail.outbox.clear()
    service.send_reminders([member_a], template=template, sent_by=group_leader)
    second = service.send_reminders([member_a], template=template, sent_by=group_leader,
                                    ignore_interval=True)
    assert second['sent'] == 1
    assert len(mail.outbox) == 2


def test_only_stale_skips_recent_reporters(db, group_leader, member_a, member_b, template):
    _entry(member_a, days_ago=1)
    _entry(member_b, days_ago=40)
    mail.outbox.clear()

    result = service.send_reminders(
        [member_a, member_b], template=template, sent_by=group_leader,
        only_stale=True, stale_days=14,
    )
    assert result['sent'] == 1
    assert result['skipped'] == 1
    assert mail.outbox[0].to == [member_b.email]


def test_dry_run_sends_nothing_and_logs_nothing(db, group_leader, member_a, template):
    mail.outbox.clear()
    result = service.send_reminders([member_a], template=template, sent_by=group_leader,
                                    dry_run=True)
    assert result['sent'] == 1
    assert mail.outbox == []
    assert ReminderLog.objects.count() == 0


def test_send_writes_an_audit_event(db, group_leader, member_a, template):
    from apps.audit.models import AuditLogEntry
    service.send_reminders([member_a], template=template, sent_by=group_leader)
    assert AuditLogEntry.objects.filter(action='reminder').exists()


def test_default_template_is_created_on_demand(db, member_a):
    assert ReminderTemplate.objects.count() == 0
    tpl = ReminderTemplate.get_default()
    assert tpl.is_default is True
    assert ReminderTemplate.get_default().pk == tpl.pk


def test_only_one_template_stays_default(db, template):
    other = ReminderTemplate.objects.create(name='other', subject='s', body='b', is_default=True)
    template.refresh_from_db()
    assert other.is_default is True
    assert template.is_default is False


# ── scheduling arithmetic ─────────────────────────────────────────────────────

def _schedule(owner, template, **kwargs):
    defaults = dict(
        name='Friday nudge', owner=owner, template=template,
        frequency=ReminderSchedule.Frequency.WEEKLY, weekday=4,
        send_time=time(15, 0), timezone='America/Chicago',
    )
    defaults.update(kwargs)
    return ReminderSchedule.objects.create(**defaults)


def test_weekly_next_fire_is_the_named_weekday_and_time(db, group_leader, template):
    s = _schedule(group_leader, template)
    tz = ZoneInfo('America/Chicago')
    # A Wednesday.
    after = datetime(2026, 9, 9, 12, 0, tzinfo=tz)
    fire = scheduling.next_fire_time(s, after=after).astimezone(tz)
    assert fire.weekday() == 4
    assert (fire.hour, fire.minute) == (15, 0)
    assert fire.date() == date(2026, 9, 11)


def test_weekly_rolls_to_next_week_when_the_slot_has_passed(db, group_leader, template):
    s = _schedule(group_leader, template)
    tz = ZoneInfo('America/Chicago')
    after = datetime(2026, 9, 11, 15, 30, tzinfo=tz)  # Friday, just after the slot
    fire = scheduling.next_fire_time(s, after=after).astimezone(tz)
    assert fire.date() == date(2026, 9, 18)


def test_biweekly_steps_a_fortnight(db, group_leader, template):
    s = _schedule(group_leader, template, frequency=ReminderSchedule.Frequency.BIWEEKLY)
    tz = ZoneInfo('America/Chicago')
    after = datetime(2026, 9, 11, 16, 0, tzinfo=tz)
    fire = scheduling.next_fire_time(s, after=after).astimezone(tz)
    assert fire.date() == date(2026, 9, 25)


def test_monthly_clamps_to_the_length_of_the_month(db, group_leader, template):
    s = _schedule(group_leader, template,
                  frequency=ReminderSchedule.Frequency.MONTHLY, day_of_month=31)
    tz = ZoneInfo('America/Chicago')
    after = datetime(2026, 2, 1, 0, 0, tzinfo=tz)
    fire = scheduling.next_fire_time(s, after=after).astimezone(tz)
    assert fire.date() == date(2026, 2, 28)


def test_local_send_time_survives_a_dst_transition(db, group_leader, template):
    """The wall clock is what the operator asked for, not a fixed UTC offset."""
    s = _schedule(group_leader, template, weekday=6, send_time=time(15, 0))
    tz = ZoneInfo('America/Chicago')

    before_dst = scheduling.next_fire_time(
        s, after=datetime(2026, 2, 20, 0, 0, tzinfo=tz)
    ).astimezone(tz)
    after_dst = scheduling.next_fire_time(
        s, after=datetime(2026, 6, 20, 0, 0, tzinfo=tz)
    ).astimezone(tz)

    assert before_dst.hour == after_dst.hour == 15
    # ... and the UTC instants therefore differ by an hour of offset.
    assert before_dst.utcoffset() != after_dst.utcoffset()


def test_unknown_timezone_falls_back_to_the_site_default(db, group_leader, template):
    s = _schedule(group_leader, template, timezone='Mars/Olympus_Mons')
    assert scheduling.next_fire_time(s) is not None


def test_saving_a_schedule_arms_next_run_at(db, group_leader, template):
    s = _schedule(group_leader, template)
    assert s.next_run_at is not None
    assert s.next_run_at > timezone.now()


# ── the atomic claim ──────────────────────────────────────────────────────────

def test_claim_succeeds_once_and_then_fails(db, group_leader, template):
    s = _schedule(group_leader, template)
    ReminderSchedule.objects.filter(pk=s.pk).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )
    s.refresh_from_db()

    # Two processes holding the same stale row; only one may win.
    other = ReminderSchedule.objects.get(pk=s.pk)
    assert scheduling.claim(s) is True
    assert scheduling.claim(other) is False


def test_claim_advances_next_run_at(db, group_leader, template):
    s = _schedule(group_leader, template)
    ReminderSchedule.objects.filter(pk=s.pk).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )
    s.refresh_from_db()
    scheduling.claim(s)
    s.refresh_from_db()
    assert s.next_run_at > timezone.now()
    assert s.last_run_at is not None


def test_run_due_only_fires_due_and_enabled_schedules(db, group_leader, member_a, template):
    due = _schedule(group_leader, template, name='due')
    ReminderSchedule.objects.filter(pk=due.pk).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )
    _schedule(group_leader, template, name='not due')
    disabled = _schedule(group_leader, template, name='disabled', is_enabled=False)
    ReminderSchedule.objects.filter(pk=disabled.pk).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )

    mail.outbox.clear()
    results = scheduling.run_due()
    assert [r['schedule'].name for r in results] == ['due']
    # The leader is a member of the group they lead, so they are reminded too.
    assert {m.to[0] for m in mail.outbox} == {member_a.email, group_leader.email}


def test_scheduled_send_respects_only_stale(db, group_leader, member_a, template):
    _entry(member_a, days_ago=1)
    _entry(group_leader, days_ago=1)
    s = _schedule(group_leader, template, only_stale=True, stale_days=14)
    ReminderSchedule.objects.filter(pk=s.pk).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )
    mail.outbox.clear()
    results = scheduling.run_due()
    assert results[0]['sent'] == 0
    assert mail.outbox == []


def test_dry_run_does_not_advance_a_schedule(db, group_leader, member_a, template):
    s = _schedule(group_leader, template)
    stale = timezone.now() - timedelta(minutes=1)
    ReminderSchedule.objects.filter(pk=s.pk).update(next_run_at=stale)

    mail.outbox.clear()
    scheduling.run_due(dry_run=True)
    s.refresh_from_db()
    assert s.next_run_at == stale
    assert mail.outbox == []


# ── management command ────────────────────────────────────────────────────────

def test_command_list_reports_schedules(db, group_leader, template, capsys):
    from django.core.management import call_command
    _schedule(group_leader, template, name='Friday nudge')
    call_command('send_reminders', '--list')
    assert 'Friday nudge' in capsys.readouterr().out


def test_command_sends_a_single_recipient(db, member_a, template):
    from django.core.management import call_command
    mail.outbox.clear()
    call_command('send_reminders', '--recipient', member_a.email)
    assert len(mail.outbox) == 1


def test_command_refuses_an_unknown_recipient(db, template):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command('send_reminders', '--recipient', 'nobody@example.com')


def test_command_force_runs_a_schedule_that_is_not_due(db, group_leader, member_a, template):
    from django.core.management import call_command
    s = _schedule(group_leader, template)
    mail.outbox.clear()
    call_command('send_reminders', '--schedule', str(s.pk), '--force')
    assert member_a.email in {m.to[0] for m in mail.outbox}


def test_command_skips_a_schedule_that_is_not_due(db, group_leader, member_a, template):
    from django.core.management import call_command
    s = _schedule(group_leader, template)
    mail.outbox.clear()
    call_command('send_reminders', '--schedule', str(s.pk))
    assert mail.outbox == []


# ── views ─────────────────────────────────────────────────────────────────────

REMINDER_GET_URLS = [
    'reminders:recipients',
    'reminders:templates',
    'reminders:template-create',
    'reminders:schedules',
    'reminders:schedule-create',
    'reminders:log',
]


@pytest.mark.parametrize('name', REMINDER_GET_URLS)
def test_pages_require_login(client, db, name):
    resp = client.get(reverse(name))
    assert resp.status_code == 302
    assert '/accounts/login/' in resp['Location']


@pytest.mark.parametrize('name', REMINDER_GET_URLS)
def test_pages_reject_regular_users(client, db, member_a, name):
    client.force_login(member_a)
    assert client.get(reverse(name)).status_code == 403


@pytest.mark.parametrize('name', REMINDER_GET_URLS)
def test_pages_reject_auditors(client, db, auditor, name):
    client.force_login(auditor)
    assert client.get(reverse(name)).status_code == 403


@pytest.mark.parametrize('role_fixture', ['functional_lead', 'group_leader',
                                          'division_head', 'admin_user'])
@pytest.mark.parametrize('name', REMINDER_GET_URLS)
def test_pages_allow_leads_and_above(client, db, request, role_fixture, name):
    user = request.getfixturevalue(role_fixture)
    client.force_login(user)
    assert client.get(reverse(name)).status_code == 200


def test_recipient_page_lists_only_people_in_scope(client, db, group_leader, member_a, member_b):
    client.force_login(group_leader)
    body = client.get(reverse('reminders:recipients')).content.decode()
    assert member_a.email in body
    assert member_b.email not in body


def test_recipient_page_query_count_is_independent_of_scope_size(
    client, db, group_leader, group_a, template
):
    """The page summarises the whole division, so it must not query per person."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def bulk(lo, hi):
        for i in range(lo, hi):
            u = User.objects.create_user(
                username=f'bulk{i}', email=f'bulk{i}@example.com', password='p', group=group_a,
            )
            _entry(u, days_ago=i + 1)

    def load():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(reverse('reminders:recipients')).status_code == 200
        return len(ctx)

    client.force_login(group_leader)
    bulk(0, 12)
    load()               # warm the session row so it is not counted below
    baseline = load()
    bulk(12, 40)
    assert load() <= baseline, 'recipient page query count grows with the scope'
    # Sanity check that the flat count is small, not merely stable.
    assert baseline < 25


def test_recipient_page_reports_counts_for_the_whole_scope(
    client, db, group_leader, group_a, member_a
):
    """Totals cover the scope even when the table shows only one page of it."""
    fresh = User.objects.create_user(username='fresh', email='fresh@example.com',
                                     password='p', group=group_a)
    _entry(fresh, days_ago=1)
    client.force_login(group_leader)
    body = client.get(reverse('reminders:recipients'), {'stale_days': '14'}).content.decode()
    # member_a and group_leader have no entries; fresh has a recent one.
    assert '<strong class="text-slate-700">3</strong> in scope' in body
    assert '<strong class="text-amber-600">2</strong>' in body


def test_recipient_page_stale_filter(client, db, group_leader, member_a):
    _entry(member_a, days_ago=1)
    client.force_login(group_leader)
    body = client.get(reverse('reminders:recipients'), {'stale': '1', 'stale_days': '14'}).content.decode()
    assert member_a.email not in body


def test_send_to_selected(client, db, group_leader, member_a, template):
    client.force_login(group_leader)
    mail.outbox.clear()
    resp = client.post(reverse('reminders:send'), {
        'mode': 'selected', 'recipient_ids': [str(member_a.pk)], 'template': str(template.pk),
    })
    assert resp.status_code == 302
    assert len(mail.outbox) == 1


def test_send_refuses_a_recipient_outside_scope(client, db, group_leader, member_b, template):
    """A hand-posted id for someone outside the scope must not be mailed."""
    client.force_login(group_leader)
    mail.outbox.clear()
    client.post(reverse('reminders:send'), {
        'mode': 'selected', 'recipient_ids': [str(member_b.pk)], 'template': str(template.pk),
    })
    assert mail.outbox == []
    assert not ReminderLog.objects.filter(recipient=member_b).exists()


def test_send_all_covers_the_whole_scope(client, db, division_head, member_a, member_b, template):
    client.force_login(division_head)
    mail.outbox.clear()
    client.post(reverse('reminders:send'), {'mode': 'all', 'template': str(template.pk)})
    recipients = {m.to[0] for m in mail.outbox}
    assert member_a.email in recipients
    assert member_b.email not in recipients


def test_send_requires_a_selection(client, db, group_leader, member_a, template):
    client.force_login(group_leader)
    mail.outbox.clear()
    resp = client.post(reverse('reminders:send'), {'mode': 'selected'})
    assert resp.status_code == 302
    assert mail.outbox == []


def test_regular_user_cannot_post_a_send(client, db, member_a, template):
    client.force_login(member_a)
    mail.outbox.clear()
    assert client.post(reverse('reminders:send'), {'mode': 'all'}).status_code == 403
    assert mail.outbox == []


def test_template_edit_records_the_editor(client, db, group_leader, template):
    client.force_login(group_leader)
    resp = client.post(reverse('reminders:template-edit', args=[template.pk]), {
        'name': 'Renamed', 'subject': 'New subject', 'body': 'New body', 'is_default': 'on',
    })
    assert resp.status_code == 302
    template.refresh_from_db()
    assert template.name == 'Renamed'
    assert template.updated_by == group_leader


def test_template_preview_renders_markdown(client, db, group_leader):
    client.force_login(group_leader)
    resp = client.post(reverse('reminders:template-preview'), {
        'subject': 'Hi {{recipient_name}}', 'body': '**bold** {{today}}',
    })
    body = resp.content.decode()
    assert '<strong>bold</strong>' in body
    assert timezone.localdate().isoformat() in body


def test_template_in_use_cannot_be_deleted(client, db, group_leader, template):
    _schedule(group_leader, template)
    ReminderTemplate.objects.create(name='spare', subject='s', body='b')
    client.force_login(group_leader)
    client.post(reverse('reminders:template-delete', args=[template.pk]))
    assert ReminderTemplate.objects.filter(pk=template.pk).exists()


def test_last_template_cannot_be_deleted(client, db, group_leader, template):
    client.force_login(group_leader)
    client.post(reverse('reminders:template-delete', args=[template.pk]))
    assert ReminderTemplate.objects.filter(pk=template.pk).exists()


def test_schedule_create_sets_owner_and_next_run(client, db, group_leader, template):
    client.force_login(group_leader)
    resp = client.post(reverse('reminders:schedule-create'), {
        'name': 'Friday 15:00', 'template': str(template.pk), 'frequency': 'weekly',
        'weekday': '4', 'day_of_month': '1', 'send_time': '15:00',
        'timezone': 'America/Chicago', 'stale_days': '14', 'only_stale': 'on',
        'is_enabled': 'on',
    })
    assert resp.status_code == 302, resp.context['form'].errors if resp.context else ''
    s = ReminderSchedule.objects.get()
    assert s.owner == group_leader
    assert s.next_run_at is not None


def test_a_lead_cannot_edit_another_leads_schedule(client, db, group_leader, division_head, template):
    s = _schedule(division_head, template, name='not yours')
    client.force_login(group_leader)
    assert client.get(reverse('reminders:schedule-edit', args=[s.pk])).status_code == 404


def test_admin_can_see_every_schedule(client, db, admin_user, group_leader, template):
    s = _schedule(group_leader, template, name='someone elses')
    client.force_login(admin_user)
    assert client.get(reverse('reminders:schedule-edit', args=[s.pk])).status_code == 200


def test_schedule_toggle_rearms_on_enable(client, db, group_leader, template):
    s = _schedule(group_leader, template)
    client.force_login(group_leader)
    client.post(reverse('reminders:schedule-toggle', args=[s.pk]))
    s.refresh_from_db()
    assert s.is_enabled is False
    client.post(reverse('reminders:schedule-toggle', args=[s.pk]))
    s.refresh_from_db()
    assert s.is_enabled is True
    assert s.next_run_at > timezone.now()


def test_run_now_sends_immediately(client, db, group_leader, member_a, template):
    s = _schedule(group_leader, template, only_stale=False)
    client.force_login(group_leader)
    mail.outbox.clear()
    client.post(reverse('reminders:schedule-run', args=[s.pk]))
    assert member_a.email in {m.to[0] for m in mail.outbox}


def test_log_page_is_scoped(client, db, group_leader, division_head, member_a, member_b, template):
    service.send_reminders([member_a], template=template, sent_by=division_head)
    service.send_reminders([member_b], template=template, sent_by=division_head)
    client.force_login(group_leader)
    body = client.get(reverse('reminders:log')).content.decode()
    assert member_a.email in body
    assert member_b.email not in body


def test_nav_shows_reminders_for_a_functional_lead(client, db, functional_lead):
    client.force_login(functional_lead)
    body = client.get(reverse('dashboard')).content.decode()
    assert reverse('reminders:recipients') in body


def test_nav_hides_reminders_from_a_regular_user(client, db, member_a):
    client.force_login(member_a)
    body = client.get(reverse('dashboard')).content.decode()
    assert 'Reminders' not in body
