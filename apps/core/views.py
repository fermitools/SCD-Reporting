import logging
import os
import subprocess

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

logger = logging.getLogger(__name__)

from .markdown import render_markdown


def _git_info():
    try:
        commit = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'], stderr=subprocess.DEVNULL
        ).decode().strip()
        date = subprocess.check_output(
            ['git', 'log', '-1', '--format=%ad', '--date=short'], stderr=subprocess.DEVNULL
        ).decode().strip()
        try:
            tag = subprocess.check_output(
                ['git', 'describe', '--tags', '--exact-match', 'HEAD'], stderr=subprocess.DEVNULL
            ).decode().strip()
        except subprocess.CalledProcessError:
            tag = ''
        return {'commit': commit, 'date': date, 'tag': tag}
    except Exception:
        # Fall back to values baked in at image build time via Docker build args.
        return {
            'commit': os.environ.get('GIT_COMMIT', 'unknown'),
            'date':   os.environ.get('GIT_DATE',   'unknown'),
            'tag':    os.environ.get('GIT_TAG',     ''),
        }


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from apps.entries.models import WorkItem
        from apps.reports.forms import AIPromptConfigForm
        from apps.reports.models import AIPromptConfig
        ctx['recent_entries'] = (
            WorkItem.objects
            .filter(author=self.request.user)
            .prefetch_related('projects', 'categories', 'tags')
            .order_by('-period_end', '-created_at')[:20]
        )
        ctx['prompt_form'] = AIPromptConfigForm(instance=AIPromptConfig.for_user(self.request.user))
        return ctx


class DashboardSummaryView(LoginRequiredMixin, View):
    def post(self, request):
        from apps.entries.models import WorkItem
        from apps.reports.ai_summary import generate

        selected_ids = request.POST.getlist('selected_ids')
        if not selected_ids:
            return render(request, 'core/partials/_dashboard_summary.html', {
                'error': 'Select at least one entry to summarize.',
            })
        qs = (WorkItem.objects
              .filter(author=request.user, pk__in=selected_ids)
              .select_related('author')
              .prefetch_related('projects', 'categories', 'tags'))
        count = qs.count()
        if count == 0:
            return render(request, 'core/partials/_dashboard_summary.html', {
                'error': 'No matching entries found.',
            })
        try:
            result = generate(qs, user=request.user)
        except Exception as exc:
            return render(request, 'core/partials/_dashboard_summary.html', {'error': str(exc)})
        return render(request, 'core/partials/_dashboard_summary.html', {
            'summary_text': result.text,
            'summary_html': render_markdown(result.text),
            'count': count,
            'truncated': result.truncated,
            'max_tokens': result.max_tokens,
        })


class DashboardSummaryDownloadTxtView(LoginRequiredMixin, View):
    def post(self, request):
        text = request.POST.get('summary_text', '')
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(text, content_type='text/plain; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="my_summary_{ts}.txt"'
        return resp


class DashboardSummaryDownloadMdView(LoginRequiredMixin, View):
    def post(self, request):
        text = request.POST.get('summary_text', '')
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(text, content_type='text/markdown; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="my_summary_{ts}.md"'
        return resp


class DashboardSummaryDownloadPdfView(LoginRequiredMixin, View):
    def post(self, request):
        from apps.reports._pdf import md_to_pdf
        text = request.POST.get('summary_text', '')
        ts_label = timezone.now().strftime('%Y-%m-%d %H:%M')
        pdf_bytes = md_to_pdf(
            markdown_text=text,
            title='SCD Activity Summary',
            meta=f'Generated {ts_label} UTC',
        )
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp = HttpResponse(pdf_bytes, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="my_summary_{ts}.pdf"'
        return resp


class DashboardPromptConfigView(LoginRequiredMixin, View):
    def post(self, request):
        from apps.reports.forms import AIPromptConfigForm
        from apps.reports.models import AIPromptConfig
        form = AIPromptConfigForm(request.POST, instance=AIPromptConfig.get_or_create_for_user(request.user))
        if form.is_valid():
            form.save()
            messages.success(request, 'AI prompt configuration saved.')
        else:
            messages.error(request, 'Could not save — check the form for errors.')
        return redirect('dashboard')


class AboutView(TemplateView):
    template_name = 'core/about.html'


class ApiView(LoginRequiredMixin, TemplateView):
    template_name = 'core/api.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['git'] = _git_info()
        return ctx


def _github_app_installation_token(app_id, private_key_pem, installation_id):
    """Exchange GitHub App credentials for a short-lived installation access token."""
    import time

    import jwt as pyjwt
    import requests as http_requests

    now = int(time.time())
    app_jwt = pyjwt.encode(
        {'iat': now - 60, 'exp': now + 600, 'iss': str(app_id)},
        private_key_pem,
        algorithm='RS256',
    )
    resp = http_requests.post(
        f'https://api.github.com/app/installations/{installation_id}/access_tokens',
        headers={
            'Authorization': f'Bearer {app_jwt}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()['token']


def _resolve_github_token():
    """
    Return a GitHub API token using the best available credentials.
    Priority: GitHub App installation token > personal access token (GITHUB_TOKEN).
    Returns None if neither is configured.
    """
    from django.conf import settings as django_settings

    app_id      = getattr(django_settings, 'GITHUB_APP_ID', '')
    install_id  = getattr(django_settings, 'GITHUB_APP_INSTALLATION_ID', '')
    private_key = getattr(django_settings, 'GITHUB_APP_PRIVATE_KEY', '')

    if app_id and install_id and private_key:
        return _github_app_installation_token(app_id, private_key, install_id)

    return getattr(django_settings, 'GITHUB_TOKEN', '') or None


class BugReportView(LoginRequiredMixin, TemplateView):
    template_name = 'core/bug_report.html'


class BugReportSubmitView(LoginRequiredMixin, View):
    REPO = 'fermitools/SCD-Reporting'
    RATE_LIMIT = 3       # max submissions per window
    RATE_WINDOW = 3600   # seconds (1 hour)
    MAX_TITLE  = 200
    MAX_BODY   = 5000

    def post(self, request):
        import requests as http_requests

        # Per-user rate limit
        cache_key = f'bug_report_count_{request.user.pk}'
        count = cache.get(cache_key, 0)
        if count >= self.RATE_LIMIT:
            messages.error(request, 'You have submitted too many bug reports recently. Please try again later.')
            return redirect('bug-report')

        title = request.POST.get('title', '').strip()[:self.MAX_TITLE]
        body  = request.POST.get('body',  '').strip()[:self.MAX_BODY]
        if not title or not body:
            messages.error(request, 'Title and description are required.')
            return redirect('bug-report')

        reporter = request.user.display_name or request.user.email
        git = _git_info()
        full_body = (
            f"{body}\n\n"
            f"---\n"
            f"**Reported by:** {reporter}  \n"
            f"**Build:** {git['commit']} ({git['date']})"
        )

        try:
            token = _resolve_github_token()
        except Exception as exc:
            logger.error('BugReport: failed to obtain GitHub token: %s', exc)
            messages.error(request, 'Bug reporting is not configured. Contact your administrator.')
            return redirect('bug-report')

        if not token:
            messages.error(request, 'Bug reporting is not configured. Contact your administrator.')
            return redirect('bug-report')

        try:
            resp = http_requests.post(
                f'https://api.github.com/repos/{self.REPO}/issues',
                json={'title': title, 'body': full_body, 'labels': ['bug']},
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': '2022-11-28',
                },
                timeout=15,
            )
            if resp.status_code == 201:
                data = resp.json()
                issue_number = data.get('number', '?')
                submitted_at = timezone.now().strftime('%Y-%m-%d %H:%M UTC')
                cache.set(cache_key, count + 1, self.RATE_WINDOW)
                messages.success(
                    request,
                    f'{reporter} successfully created issue #{issue_number} at {submitted_at}.',
                )
                return redirect('about')
            else:
                logger.error('BugReport: GitHub API returned %s: %s', resp.status_code, resp.text[:500])
                messages.error(request, 'Bug report could not be submitted. Please try again or contact your administrator.')
        except Exception as exc:
            logger.error('BugReport: error posting to GitHub: %s', exc)
            messages.error(request, 'Bug report could not be submitted. Please try again or contact your administrator.')

        return redirect('bug-report')
