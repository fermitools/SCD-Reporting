from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from apps.accounts.permissions import EntryManagerRequiredMixin
from apps.core.markdown import render_markdown
from apps.taxonomy.models import Project, Tag

from .forms import WorkItemForm
from .models import EntryTemplate, WorkItem


# ── List ──────────────────────────────────────────────────────────────────────

class EntryListView(LoginRequiredMixin, ListView):
    model = WorkItem
    template_name = 'entries/list.html'
    context_object_name = 'entries'
    paginate_by = 25

    def get_queryset(self):
        qs = (WorkItem.objects
              .filter(author=self.request.user, is_archived=False)
              .prefetch_related('projects', 'categories', 'tags'))
        project_id = self.request.GET.get('project')
        if project_id:
            qs = qs.filter(projects__id=project_id)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(description__icontains=q)
                | Q(tags__name__icontains=q)
            )
        if project_id or q:
            qs = qs.distinct()
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['projects'] = Project.objects.filter(is_active=True).order_by('sort_order', 'name')
        ctx['selected_project'] = self.request.GET.get('project', '')
        ctx['search_query'] = self.request.GET.get('q', '').strip()
        return ctx


# ── Create ────────────────────────────────────────────────────────────────────

def _user_entry_templates(user):
    return list(EntryTemplate.objects.filter(user=user).values('pk', 'name', 'body'))


class EntryCreateView(LoginRequiredMixin, CreateView):
    model = WorkItem
    form_class = WorkItemForm
    template_name = 'entries/form.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['entry_templates'] = _user_entry_templates(self.request.user)
        return ctx

    def get_initial(self):
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        initial = {
            'period_kind':  'week',
            'period_start': week_start.isoformat(),
            'period_end':   (week_start + timedelta(days=6)).isoformat(),
        }
        if self.request.user.group_id:
            initial['group'] = self.request.user.group_id
        prefill_desc = self.request.session.pop('prefill_description', None)
        if prefill_desc is not None:
            initial['description'] = prefill_desc
            initial.setdefault('title', 'AI Summary')
        return initial

    def form_valid(self, form):
        form.instance.author = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f'Entry "{self.object.title}" submitted.')
        return response

    def get_success_url(self):
        return reverse('entries:detail', kwargs={'pk': self.object.pk})


# ── Detail ────────────────────────────────────────────────────────────────────

class EntryDetailView(LoginRequiredMixin, DetailView):
    model = WorkItem
    template_name = 'entries/detail.html'

    def get_queryset(self):
        qs = WorkItem.objects.prefetch_related('projects', 'categories', 'lab_priorities', 'tags')
        user = self.request.user
        if user.is_scd_admin or user.is_division_head:
            pass  # full access including division-head-only entries
        elif user.is_auditor or user.is_group_leader or user.is_functional_lead:
            qs = qs.filter(Q(is_division_head_only=False) | Q(author=user))
        else:
            qs = qs.filter(author=user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['description_html'] = render_markdown(self.object.description)
        return ctx


# ── Edit ──────────────────────────────────────────────────────────────────────

class EntryUpdateView(LoginRequiredMixin, UpdateView):
    model = WorkItem
    form_class = WorkItemForm
    template_name = 'entries/form.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['entry_templates'] = _user_entry_templates(self.request.user)
        return ctx

    def get_queryset(self):
        return WorkItem.objects.filter(author=self.request.user)

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, 'Entry updated.')
        return response

    def get_success_url(self):
        return reverse('entries:detail', kwargs={'pk': self.object.pk})


# ── Delete ────────────────────────────────────────────────────────────────────

class EntryDeleteView(LoginRequiredMixin, DeleteView):
    model = WorkItem
    template_name = 'entries/confirm_delete.html'
    success_url = reverse_lazy('entries:list')

    def get_queryset(self):
        return WorkItem.objects.filter(author=self.request.user)

    def form_valid(self, form):
        tag_ids = list(self.object.tags.values_list('id', flat=True))
        title = self.object.title
        response = super().form_valid(form)
        if tag_ids:
            Tag.objects.filter(id__in=tag_ids).update(use_count=F('use_count') - 1)
        messages.success(self.request, f'Entry "{title}" deleted.')
        return response


# ── Create from AI summary ────────────────────────────────────────────────────

class EntryCreateFromSummaryView(LoginRequiredMixin, View):
    def post(self, request):
        request.session['prefill_description'] = request.POST.get('summary_text', '')
        return redirect(reverse('entries:create'))


# ── HTMX: period prefill ──────────────────────────────────────────────────────

class PeriodPrefillView(LoginRequiredMixin, View):
    def get(self, request):
        kind  = request.GET.get('kind', 'week')
        today = date.today()
        if kind == 'today':
            start = end = today
        elif kind == 'week':
            start = today - timedelta(days=today.weekday())
            end   = start + timedelta(days=6)
        elif kind == 'fortnight':
            start = today - timedelta(days=13)
            end   = today
        elif kind == 'month':
            start = today.replace(day=1)
            end   = today
        else:
            start = end = today
        return render(request, 'entries/partials/_period_inputs.html', {
            'period_start': start.isoformat(),
            'period_end':   end.isoformat(),
        })


# ── HTMX: markdown preview ────────────────────────────────────────────────────

class MarkdownPreviewView(LoginRequiredMixin, View):
    def post(self, request):
        text = request.POST.get('description', '')
        return render(request, 'entries/partials/_markdown_preview.html', {
            'html': render_markdown(text),
        })


# ── Entry management (admin / division head / group leader) ───────────────────

class EntryManageView(EntryManagerRequiredMixin, ListView):
    model = WorkItem
    template_name = 'entries/manage.html'
    context_object_name = 'entries'
    paginate_by = 50

    def get_queryset(self):
        from apps.accounts.models import User
        qs = (WorkItem.objects
              .select_related('author')
              .prefetch_related('projects', 'categories', 'tags'))
        user = self.request.user
        if not (user.is_scd_admin or user.is_division_head):
            qs = qs.filter(is_division_head_only=False)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(title__icontains=q)
        author = self.request.GET.get('author', '').strip()
        if author:
            qs = qs.filter(author__email__icontains=author)
        project = self.request.GET.get('project', '').strip()
        if project:
            qs = qs.filter(projects__id=project).distinct()
        show_archived = self.request.GET.get('archived') == '1'
        if show_archived:
            qs = qs.filter(is_archived=True)
        else:
            qs = qs.filter(is_archived=False)
        return qs.order_by('-period_end', 'author__email')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['projects'] = Project.objects.filter(is_active=True).order_by('sort_order', 'name')
        ctx['show_archived'] = self.request.GET.get('archived') == '1'
        from apps.accounts.models import User
        ctx['all_users'] = User.objects.order_by('email')
        return ctx


def _managed_entry_or_404(user, pk):
    """Resolve an entry for a manager action, honouring the same visibility rule
    as EntryManageView: only admins and division heads may touch entries flagged
    division-head-only. Anything else, including an unknown pk, is a 404 rather
    than a 500 or a silent privilege escalation (GitHub #12)."""
    qs = WorkItem.objects.all()
    if not (user.is_scd_admin or user.is_division_head):
        qs = qs.filter(is_division_head_only=False)
    return get_object_or_404(qs, pk=pk)


class EntryReassignView(EntryManagerRequiredMixin, View):
    def post(self, request, pk):
        from apps.accounts.models import User
        entry = _managed_entry_or_404(request.user, pk)
        new_author_id = request.POST.get('author_id')
        try:
            new_author = User.objects.get(pk=new_author_id)
        except User.DoesNotExist:
            messages.error(request, 'User not found.')
            return redirect('entries:manage')
        old_email = entry.author.email
        entry.author = new_author
        entry.save(update_fields=['author', 'updated_at'])
        messages.success(request, f'Entry "{entry.title}" reassigned from {old_email} to {new_author.email}.')
        return redirect(request.POST.get('next', reverse('entries:manage')))


class EntryArchiveView(EntryManagerRequiredMixin, View):
    def post(self, request, pk):
        entry = _managed_entry_or_404(request.user, pk)
        entry.is_archived = not entry.is_archived
        entry.save(update_fields=['is_archived', 'updated_at'])
        action = 'archived' if entry.is_archived else 'unarchived'
        messages.success(request, f'Entry "{entry.title}" {action}.')
        return redirect(request.POST.get('next', reverse('entries:manage')))


class EntryManagerDeleteView(EntryManagerRequiredMixin, View):
    def post(self, request, pk):
        entry = _managed_entry_or_404(request.user, pk)
        tag_ids = list(entry.tags.values_list('id', flat=True))
        title = entry.title
        entry.delete()
        if tag_ids:
            Tag.objects.filter(id__in=tag_ids).update(use_count=F('use_count') - 1)
        messages.success(request, f'Entry "{title}" permanently deleted.')
        return redirect(request.POST.get('next', reverse('entries:manage')))


# ── HTMX: entry description templates ────────────────────────────────────────

class EntryTemplateDeleteView(LoginRequiredMixin, View):
    """JSON endpoint: delete a named description template owned by the current user."""

    def post(self, request):
        from django.http import JsonResponse
        pk = request.POST.get('pk', '').strip()
        if not pk:
            return JsonResponse({'ok': False, 'error': 'No template specified.'}, status=400)
        try:
            tpl = EntryTemplate.objects.get(pk=pk, user=request.user)
        except EntryTemplate.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Template not found.'}, status=404)
        name = tpl.name
        tpl.delete()
        return JsonResponse({'ok': True, 'pk': int(pk), 'name': name})


class EntryTemplateSaveView(LoginRequiredMixin, View):
    """JSON endpoint: save or save-as a named description template for the current user."""

    def post(self, request):
        import json as _json
        body = request.POST.get('body', '').strip()
        name = request.POST.get('name', '').strip()
        pk   = request.POST.get('pk', '').strip()

        if name:
            # Save As: create new (or update if name already exists for this user)
            if not name:
                return self._err('Template name is required.')
            obj, created = EntryTemplate.objects.get_or_create(
                user=request.user, name=name,
                defaults={'body': body},
            )
            if not created:
                obj.body = body
                obj.save(update_fields=['body'])
            return self._ok({'pk': obj.pk, 'name': obj.name, 'created': created})
        elif pk:
            # Save: update existing template owned by this user
            try:
                obj = EntryTemplate.objects.get(pk=pk, user=request.user)
            except EntryTemplate.DoesNotExist:
                return self._err('Template not found.')
            obj.body = body
            obj.save(update_fields=['body'])
            return self._ok({'pk': obj.pk, 'name': obj.name, 'created': False})
        else:
            return self._err('No template selected and no name provided.')

    @staticmethod
    def _ok(data):
        from django.http import JsonResponse
        return JsonResponse({'ok': True, **data})

    @staticmethod
    def _err(msg):
        from django.http import JsonResponse
        return JsonResponse({'ok': False, 'error': msg}, status=400)
