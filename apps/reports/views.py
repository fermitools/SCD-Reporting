from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.accounts.permissions import AdminRequiredMixin, AuditorOrAdminRequiredMixin
from apps.core.markdown import render_markdown
from apps.entries.models import WorkItem

from . import ai_summary, exporters
from .filters import WorkItemFilter
from .forms import AIPromptConfigForm, NamedPromptTemplateForm
from .models import AIPromptConfig, NamedPromptTemplate

PREVIEW_LIMIT = 50


def _get_group_scope(user):
    """Return a WorkGroup queryset to restrict reports to, or None for no restriction."""
    # Admins previewing another role have no real managed groups/projects assigned,
    # so skip scope restrictions and show all data.
    if getattr(user, '_is_previewing', False):
        return None
    from apps.taxonomy.models import WorkGroup
    if user.is_group_leader:
        return WorkGroup.objects.filter(pk=user.group_id) if user.group_id else WorkGroup.objects.none()
    if user.is_division_head:
        return user.managed_groups.all()
    return None


def _get_project_scope(user):
    """Return a Project queryset to restrict reports to, or None for no restriction."""
    if getattr(user, '_is_previewing', False):
        return None
    if user.is_functional_lead:
        return user.managed_projects.all()
    return None


def _filtered_qs(data, group_scope=None, project_scope=None, user=None):
    qs = WorkItem.objects.select_related('author').prefetch_related('projects', 'categories', 'tags')
    if group_scope is not None:
        # Prefer explicit WorkItem.group; fall back to author.group only when entry has no group set.
        qs = qs.filter(Q(group__in=group_scope) | Q(group__isnull=True, author__group__in=group_scope))
    if project_scope is not None:
        qs = qs.filter(projects__in=project_scope).distinct()
    if user is not None and not (user.is_scd_admin or user.is_division_head):
        qs = qs.filter(is_division_head_only=False)
    show_archived = data.get('show_archived') == '1'
    if show_archived:
        qs = qs.filter(is_archived=True)
    else:
        qs = qs.filter(is_archived=False)
    f = WorkItemFilter(data, queryset=qs)
    return f, f.qs.order_by('-period_end', 'author__email')


def _user_templates(user):
    return NamedPromptTemplate.objects.filter(user=user)


class ReportIndexView(AuditorOrAdminRequiredMixin, View):
    def get(self, request):
        f = WorkItemFilter(queryset=WorkItem.objects.none())
        return render(request, 'reports/index.html', {
            'filter': f,
            'formats': exporters.available(),
            'prompt_form': AIPromptConfigForm(instance=AIPromptConfig.for_user(request.user)),
            'user_templates': _user_templates(request.user),
            'group_scope': _get_group_scope(request.user),
            'project_scope': _get_project_scope(request.user),
        })


class ReportPreviewView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        group_scope   = _get_group_scope(request.user)
        project_scope = _get_project_scope(request.user)
        f, qs = _filtered_qs(request.POST, group_scope=group_scope, project_scope=project_scope, user=request.user)
        total = qs.count()
        rows  = qs[:PREVIEW_LIMIT]
        return render(request, 'reports/partials/_preview.html', {
            'filter':  f,
            'rows':    rows,
            'total':   total,
            'limit':   PREVIEW_LIMIT,
        })


class ReportDownloadView(AuditorOrAdminRequiredMixin, View):
    def post(self, request, fmt: str):
        exporter = exporters.get(fmt)
        if not exporter:
            from django.http import HttpResponseBadRequest
            return HttpResponseBadRequest(f'Unknown format: {fmt}')
        group_scope   = _get_group_scope(request.user)
        project_scope = _get_project_scope(request.user)
        _, qs = _filtered_qs(request.POST, group_scope=group_scope, project_scope=project_scope, user=request.user)
        selected_ids = request.POST.getlist('selected_ids')
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        from apps.audit.service import log_event
        log_event(
            action='export',
            request=request,
            changes={'format': fmt, 'count': qs.count(), 'selection': bool(selected_ids)},
        )
        return exporter(qs)


class ReportSummaryView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        group_scope   = _get_group_scope(request.user)
        project_scope = _get_project_scope(request.user)
        _, qs = _filtered_qs(request.POST, group_scope=group_scope, project_scope=project_scope, user=request.user)
        selected_ids = request.POST.getlist('selected_ids')
        if selected_ids:
            qs = qs.filter(pk__in=selected_ids)
        count = qs.count()
        if count == 0:
            return render(request, 'reports/partials/_summary.html', {
                'error': 'No entries matched. Adjust filters or select rows in the preview first.',
            })

        # Use named template if one was selected in the prompt config panel.
        named_template = None
        try:
            pk = int(request.POST.get('selected_template_pk', '') or 0)
            if pk:
                named_template = NamedPromptTemplate.objects.get(pk=pk, user=request.user)
        except (ValueError, NamedPromptTemplate.DoesNotExist):
            pass

        try:
            result = ai_summary.generate(qs, user=request.user, template=named_template)
        except Exception as exc:
            return render(request, 'reports/partials/_summary.html', {'error': str(exc)})
        from apps.audit.service import log_event
        log_event(
            action='export',
            request=request,
            changes={
                'format': 'ai_summary', 'count': count, 'selection': bool(selected_ids),
                'output_tokens': result.output_tokens, 'truncated': result.truncated,
            },
        )
        return render(request, 'reports/partials/_summary.html', {
            'summary_text': result.text,
            'summary_html': render_markdown(result.text),
            'count': count,
            'truncated': result.truncated,
            'max_tokens': result.max_tokens,
        })


class SummaryDownloadTxtView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        text = request.POST.get('summary_text', '')
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(text, content_type='text/plain; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="scd_summary_{ts}.txt"'
        return resp


class SummaryDownloadMdView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        text = request.POST.get('summary_text', '')
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(text, content_type='text/markdown; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="scd_summary_{ts}.md"'
        return resp


class SummaryDownloadPdfView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        from ._pdf import md_to_pdf

        text = request.POST.get('summary_text', '')
        ts_label = timezone.now().strftime('%Y-%m-%d %H:%M')
        pdf_bytes = md_to_pdf(
            markdown_text=text,
            title='SCD Effort Report — AI Summary',
            meta=f'Generated {ts_label} UTC',
        )
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="scd_summary_{ts}.pdf"'
        return resp


class AIPromptConfigView(AuditorOrAdminRequiredMixin, View):
    def post(self, request):
        save_as_name = request.POST.get('save_as_name', '').strip()
        template_pk  = request.POST.get('template_pk', '').strip()

        if save_as_name:
            # Save As: create a new named template owned by this user.
            obj = NamedPromptTemplate(user=request.user)
            # Override name from the dedicated save_as_name field.
            data = request.POST.copy()
            data['name'] = save_as_name
            form = NamedPromptTemplateForm(data, instance=obj)
            if form.is_valid():
                form.save()
                messages.success(request, f'Saved new template "{save_as_name}".')
            else:
                for field, errs in form.errors.items():
                    for e in errs:
                        messages.error(request, f'{field}: {e}')
        elif template_pk:
            # Save: update an existing named template (name is preserved, not re-submitted).
            try:
                obj = NamedPromptTemplate.objects.get(pk=template_pk, user=request.user)
            except NamedPromptTemplate.DoesNotExist:
                messages.error(request, 'Template not found.')
                return redirect('reports:index')
            data = request.POST.copy()
            data['name'] = obj.name
            form = NamedPromptTemplateForm(data, instance=obj)
            if form.is_valid():
                form.save()
                messages.success(request, f'Template "{obj.name}" updated.')
            else:
                for field, errs in form.errors.items():
                    for e in errs:
                        messages.error(request, f'{field}: {e}')
        else:
            # Save the user's default config (or global for admins).
            if request.user.is_scd_admin:
                instance = AIPromptConfig.get_solo()
            else:
                instance = AIPromptConfig.get_or_create_for_user(request.user)
            form = AIPromptConfigForm(request.POST, instance=instance)
            if form.is_valid():
                form.save()
                messages.success(request, 'Default AI prompt configuration saved.')
            else:
                messages.error(request, 'Could not save — check the form for errors.')

        return redirect('reports:index')


class PromptTemplateLoadView(AuditorOrAdminRequiredMixin, View):
    """Return the prompt fields partial for HTMX swap when the user selects a template."""

    def get(self, request, pk):
        if pk == 0:
            config = AIPromptConfig.for_user(request.user)
            system_prompt = config.system_prompt
            user_template = config.user_template
            template_name = ''
        else:
            tpl = get_object_or_404(NamedPromptTemplate, pk=pk, user=request.user)
            system_prompt = tpl.system_prompt
            user_template = tpl.user_template
            template_name = tpl.name
        return render(request, 'reports/partials/_prompt_fields.html', {
            'system_prompt': system_prompt,
            'user_template': user_template,
            'template_name': template_name,
        })


class PromptTemplateDeleteView(AuditorOrAdminRequiredMixin, View):
    """Delete the requesting user's named prompt template."""

    def post(self, request, pk):
        tpl = get_object_or_404(NamedPromptTemplate, pk=pk, user=request.user)
        name = tpl.name
        tpl.delete()
        messages.success(request, f'Template "{name}" deleted.')
        return redirect('reports:index')


class PromptTemplateAdminView(AdminRequiredMixin, View):
    """Admin page: list all users' named prompt templates with delete controls."""

    def get(self, request):
        templates = (
            NamedPromptTemplate.objects
            .select_related('user')
            .order_by('user__email', 'name')
        )
        return render(request, 'reports/prompt_admin.html', {'templates': templates})


class PromptTemplateAdminDeleteView(AdminRequiredMixin, View):
    """Admin: delete any user's named prompt template."""

    def post(self, request, pk):
        tpl = get_object_or_404(NamedPromptTemplate, pk=pk)
        name = tpl.name
        owner = tpl.user.email
        tpl.delete()
        messages.success(request, f'Deleted template "{name}" (owned by {owner}).')
        return redirect('reports:prompt-templates-admin')
