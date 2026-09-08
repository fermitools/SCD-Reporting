"""Reminder pages: recipients, templates, schedules, and the send log."""

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.accounts.permissions import ReminderSenderRequiredMixin
from apps.taxonomy.models import Project, WorkGroup

from . import scheduling, scope, service
from .forms import ReminderScheduleForm, ReminderTemplateForm
from .models import ReminderLog, ReminderTemplate

RECIPIENTS_PER_PAGE = 100


def _stale_days(request):
    raw = (request.POST.get('stale_days') or request.GET.get('stale_days') or '').strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return settings.REMINDER_STALE_DAYS


class RecipientListView(ReminderSenderRequiredMixin, View):
    """The people in the requester's scope, with their reporting freshness."""

    def get(self, request):
        qs = scope.recipient_qs(request.user).select_related('group')

        search = (request.GET.get('q') or '').strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(display_name__icontains=search)
                | Q(username__icontains=search)
            )

        group_id = (request.GET.get('group') or '').strip()
        if group_id:
            qs = qs.filter(group_id=group_id)

        project_id = (request.GET.get('project') or '').strip()
        if project_id:
            qs = qs.filter(work_items__projects__id=project_id).distinct()

        stale_days = _stale_days(request)
        stale_only = request.GET.get('stale') == '1'

        qs = service.annotate_freshness(qs, stale_days).order_by('email')
        stale_filter = service.stale_q(stale_days)
        # Counted in SQL: the header summarises the whole scope, which may be
        # the entire division, while the table only renders one page of it.
        stale_count = qs.filter(stale_filter).count()
        if stale_only:
            qs = qs.filter(stale_filter)
        total = qs.count()

        page = Paginator(qs, RECIPIENTS_PER_PAGE).get_page(request.GET.get('page'))
        rows = service.rows_from_annotated(page.object_list, stale_days)

        return render(request, 'reminders/recipients.html', {
            'rows': rows,
            'page_obj': page,
            'is_paginated': page.has_other_pages(),
            'total': total,
            'stale_count': stale_count,
            'stale_days': stale_days,
            'stale_only': stale_only,
            'scope_description': scope.scope_description(request.user),
            'groups': WorkGroup.objects.filter(is_active=True).order_by('sort_order', 'name'),
            'projects': _project_choices(request.user),
            'templates': ReminderTemplate.objects.all(),
            'default_template': ReminderTemplate.objects.filter(is_default=True).first(),
            'min_interval_hours': settings.REMINDER_MIN_INTERVAL_HOURS,
            'base_url_configured': bool(service.base_url()),
        })


def _project_choices(user):
    if user.is_functional_lead and not getattr(user, '_is_previewing', False):
        return user.managed_projects.filter(is_active=True).order_by('sort_order', 'name')
    return Project.objects.filter(is_active=True).order_by('sort_order', 'name')


class SendReminderView(ReminderSenderRequiredMixin, View):
    """Send reminders to selected recipients, or to everyone in scope."""

    def post(self, request):
        allowed = scope.recipient_qs(request.user)

        mode = request.POST.get('mode', 'selected')
        if mode == 'all':
            recipients = list(allowed.order_by('email'))
        elif mode == 'stale':
            stale_days = _stale_days(request)
            recipients = [
                u for u in allowed.order_by('email')
                if service.is_stale(service.recipient_facts(u), stale_days)
            ]
        else:
            ids = request.POST.getlist('recipient_ids')
            if not ids:
                messages.error(request, 'Select at least one person to remind.')
                return redirect(request.POST.get('next') or 'reminders:recipients')
            # Filtering through `allowed` is what enforces the scope — an id
            # posted by hand for someone outside it simply does not match.
            recipients = list(allowed.filter(pk__in=ids).order_by('email'))

        if not recipients:
            messages.warning(request, 'Nobody in your scope matched — no reminders sent.')
            return redirect(request.POST.get('next') or 'reminders:recipients')

        template = None
        template_id = (request.POST.get('template') or '').strip()
        if template_id:
            template = get_object_or_404(ReminderTemplate, pk=template_id)

        result = service.send_reminders(
            recipients,
            template=template,
            sent_by=request.user,
            request=request,
            only_stale=(mode == 'stale'),
            stale_days=_stale_days(request),
            ignore_interval=request.POST.get('ignore_interval') == '1',
        )

        parts = [f"{result['sent']} reminder(s) sent"]
        if result['skipped']:
            parts.append(f"{result['skipped']} skipped")
        if result['failed']:
            parts.append(f"{result['failed']} failed")
        summary = ', '.join(parts) + '.'
        if result['failed']:
            messages.error(request, summary + ' See the reminder log for details.')
        elif result['sent']:
            messages.success(request, summary)
        else:
            messages.warning(request, summary + ' Check the skip reasons in the reminder log.')

        return redirect(request.POST.get('next') or 'reminders:recipients')


# ── Templates ─────────────────────────────────────────────────────────────────

class TemplateListView(ReminderSenderRequiredMixin, View):
    def get(self, request):
        # Materialise the built-in template on first visit so the page is never
        # empty and a send always has something to use.
        ReminderTemplate.get_default()
        return render(request, 'reminders/templates.html', {
            'templates': ReminderTemplate.objects.select_related('updated_by'),
            'placeholders': service.PLACEHOLDERS,
        })


class TemplateEditView(ReminderSenderRequiredMixin, View):
    """Create (pk=None) or edit one template."""

    def _instance(self, pk):
        return get_object_or_404(ReminderTemplate, pk=pk) if pk else None

    def get(self, request, pk=None):
        instance = self._instance(pk)
        return render(request, 'reminders/template_form.html', {
            'form': ReminderTemplateForm(instance=instance),
            'object': instance,
            'placeholders': service.PLACEHOLDERS,
        })

    def post(self, request, pk=None):
        instance = self._instance(pk)
        form = ReminderTemplateForm(request.POST, instance=instance)
        if not form.is_valid():
            return render(request, 'reminders/template_form.html', {
                'form': form,
                'object': instance,
                'placeholders': service.PLACEHOLDERS,
            })
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        messages.success(request, f'Template "{obj.name}" saved.')
        return redirect('reminders:templates')


class TemplateDeleteView(ReminderSenderRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(ReminderTemplate, pk=pk)
        if obj.schedules.exists():
            messages.error(
                request,
                f'"{obj.name}" is used by a schedule. Point the schedule at another '
                'template first.',
            )
            return redirect('reminders:templates')
        if ReminderTemplate.objects.count() <= 1:
            messages.error(request, 'At least one template must remain.')
            return redirect('reminders:templates')
        name = obj.name
        obj.delete()
        messages.success(request, f'Template "{name}" deleted.')
        return redirect('reminders:templates')


class TemplateDefaultView(ReminderSenderRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(ReminderTemplate, pk=pk)
        obj.is_default = True
        obj.save()
        messages.success(request, f'"{obj.name}" is now the default template.')
        return redirect('reminders:templates')


class TemplatePreviewView(ReminderSenderRequiredMixin, View):
    """HTMX preview: substitute placeholders against the requester, render Markdown."""

    def post(self, request):
        body = request.POST.get('body', '')
        subject = request.POST.get('subject', '')

        class _Draft:
            pass
        draft = _Draft()
        draft.subject = subject
        draft.body = body

        subject_out, text, html = service.render_reminder(
            draft, request.user, sender_name=request.user.display_name or request.user.email,
            request=request,
        )
        return render(request, 'reminders/partials/_preview.html', {
            'subject': subject_out,
            'html': html,
            'text': text,
        })


# ── Schedules ─────────────────────────────────────────────────────────────────

class ScheduleListView(ReminderSenderRequiredMixin, View):
    def get(self, request):
        return render(request, 'reminders/schedules.html', {
            'schedules': scope.visible_schedules(request.user),
            'scheduler_enabled': settings.REMINDER_SCHEDULER_ENABLED,
            'base_url_configured': bool(service.base_url()),
        })


class ScheduleEditView(ReminderSenderRequiredMixin, View):
    def _instance(self, request, pk):
        if not pk:
            return None
        return get_object_or_404(scope.visible_schedules(request.user), pk=pk)

    def get(self, request, pk=None):
        instance = self._instance(request, pk)
        ReminderTemplate.get_default()
        return render(request, 'reminders/schedule_form.html', {
            'form': ReminderScheduleForm(instance=instance),
            'object': instance,
        })

    def post(self, request, pk=None):
        instance = self._instance(request, pk)
        form = ReminderScheduleForm(request.POST, instance=instance)
        if not form.is_valid():
            return render(request, 'reminders/schedule_form.html', {
                'form': form,
                'object': instance,
            })
        obj = form.save(commit=False)
        if obj.owner_id is None:
            # The owner is the scope source, so a schedule always belongs to the
            # lead who created it, never to whoever last edited it.
            obj.owner = request.user
        obj.save()
        messages.success(
            request,
            f'Schedule "{obj.name}" saved — next run {obj.next_run_at:%Y-%m-%d %H:%M} UTC.',
        )
        return redirect('reminders:schedules')


class ScheduleToggleView(ReminderSenderRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(scope.visible_schedules(request.user), pk=pk)
        obj.is_enabled = not obj.is_enabled
        if obj.is_enabled:
            # Re-arm from now rather than firing immediately for the time it was
            # disabled over.
            obj.next_run_at = scheduling.next_fire_time(obj)
        obj.save(update_fields=['is_enabled', 'next_run_at'])
        messages.success(
            request,
            f'Schedule "{obj.name}" {"enabled" if obj.is_enabled else "disabled"}.',
        )
        return redirect('reminders:schedules')


class ScheduleDeleteView(ReminderSenderRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(scope.visible_schedules(request.user), pk=pk)
        name = obj.name
        obj.delete()
        messages.success(request, f'Schedule "{name}" deleted.')
        return redirect('reminders:schedules')


class ScheduleRunNowView(ReminderSenderRequiredMixin, View):
    """Fire a schedule immediately, without waiting for its next slot."""

    def post(self, request, pk):
        obj = get_object_or_404(scope.visible_schedules(request.user), pk=pk)
        dry_run = request.POST.get('dry_run') == '1'
        results = scheduling.run_due(force_schedule=obj, dry_run=dry_run)
        outcome = results[0] if results else {'sent': 0, 'skipped': 0, 'failed': 0}
        prefix = 'Dry run: ' if dry_run else ''
        messages.success(
            request,
            f"{prefix}{obj.name} — {outcome['sent']} sent, "
            f"{outcome['skipped']} skipped, {outcome['failed']} failed.",
        )
        return redirect('reminders:schedules')


# ── Log ───────────────────────────────────────────────────────────────────────

class LogView(ReminderSenderRequiredMixin, View):
    def get(self, request):
        qs = scope.visible_log(request.user)

        status = (request.GET.get('status') or '').strip()
        if status:
            if status not in ReminderLog.Status.values:
                return HttpResponseBadRequest('Unknown status')
            qs = qs.filter(status=status)

        search = (request.GET.get('q') or '').strip()
        if search:
            qs = qs.filter(to_email__icontains=search)

        page = Paginator(qs, 100).get_page(request.GET.get('page'))
        return render(request, 'reminders/log.html', {
            'rows': page.object_list,
            'page_obj': page,
            'is_paginated': page.has_other_pages(),
            'status_choices': ReminderLog.Status.choices,
            'status': status,
        })
