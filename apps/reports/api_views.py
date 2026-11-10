"""Token-authenticated report API.

GET|POST /api/reports/          — run the Reports-page filter and export it
GET|POST /api/reports/summary/  — AI summary of the same selection

Both use the exact scoping of the Reports page (group scope for Group Leaders
and Division Heads, project scope for Functional Leads, division-head-only
entries hidden unless Admin or Division Head) by calling the same helpers.
"""
import logging

from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from apps.audit.service import log_event
from apps.core.api_auth import TokenAPIView, json_error
from apps.core.api_params import (
    ParamError, id_list, is_true, request_params, resolve_taxonomy,
)
from apps.taxonomy.models import Category, EntryType, LabPriority, Project, WorkGroup

from . import ai_summary, exporters
from .models import NamedPromptTemplate
from .views import _filtered_qs, _get_group_scope, _get_project_scope

logger = logging.getLogger(__name__)

REPORT_TAXONOMY_FIELDS = {
    'projects':       Project,
    'categories':     Category,
    'lab_priorities': LabPriority,
    'group':          WorkGroup,
    'employee_group': WorkGroup,
    'entry_type':     EntryType,
}

# Accept the shorter/singular spellings a CLI user is likely to type.
PARAM_ALIASES = {
    'project':      'projects',
    'category':     'categories',
    'lab_priority': 'lab_priorities',
    'author':       'author_email',
    'ids':          'selected_ids',
    'id':           'selected_ids',
}


def _apply_aliases(params):
    for alias, canonical in PARAM_ALIASES.items():
        if alias in params:
            params.setlist(canonical, params.getlist(canonical) + params.getlist(alias))
            del params[alias]
    return params


def report_queryset(request, params):
    """Return (queryset, selection_used) for the report filters in ``params``.

    Raises ParamError for unknown slugs or invalid filter values.
    """
    _apply_aliases(params)
    resolve_taxonomy(params, REPORT_TAXONOMY_FIELDS)
    params['show_archived'] = '1' if is_true(params, 'show_archived') else ''
    user = request.user
    f, qs = _filtered_qs(
        params,
        group_scope=_get_group_scope(user),
        project_scope=_get_project_scope(user),
        user=user,
    )
    if not f.is_valid():
        raise ParamError({k: '; '.join(v) for k, v in f.errors.items()})
    selected = id_list(params, 'selected_ids')
    if selected:
        qs = qs.filter(pk__in=selected)
    return qs, bool(selected)


class ReportAPIView(TokenAPIView):
    require_reporter = True

    def get(self, request):
        return self._run(request)

    def post(self, request):
        return self._run(request)

    def _run(self, request):
        try:
            params = request_params(request)
            fmt = (params.get('format') or 'json').lower()
            count_only = is_true(params, 'count')
            exporter = exporters.get(fmt)
            if not count_only and exporter is None:
                raise ParamError({'format': f'Unknown format "{fmt}". '
                                            f'Available: {", ".join(exporters.available())}.'})
            qs, selection = report_queryset(request, params)
        except ParamError as exc:
            return JsonResponse({'errors': exc.errors}, status=400)

        count = qs.count()
        if count_only:
            return JsonResponse({'count': count})
        log_event(
            action='export', actor=request.user, request=request,
            changes={'format': fmt, 'count': count, 'selection': selection, 'source': 'api'},
        )
        return exporter(qs)


def _resolve_template(user, value):
    if not value:
        return None
    qs = NamedPromptTemplate.objects.filter(user=user)
    tmpl = qs.filter(pk=int(value)).first() if value.isdigit() else qs.filter(name=value).first()
    if tmpl is None:
        raise ParamError({'template': f'No saved prompt template "{value}" for this user.'})
    return tmpl


class ReportSummaryAPIView(TokenAPIView):
    """AI summary. POST only: each call is a paid model request."""

    require_reporter = True

    def post(self, request):
        try:
            params = request_params(request)
            fmt = (params.get('format') or 'json').lower()
            if fmt not in ('json', 'md', 'txt'):
                raise ParamError({'format': 'Summary format must be json, md or txt.'})
            template = _resolve_template(request.user, params.get('template', '').strip())
            qs, selection = report_queryset(request, params)
        except ParamError as exc:
            return JsonResponse({'errors': exc.errors}, status=400)

        count = qs.count()
        if count == 0:
            return json_error('No entries matched the filters.', status=400)
        try:
            result = ai_summary.generate(qs, user=request.user, template=template)
        except ValueError as exc:   # configuration / input-size problems
            return json_error(str(exc), status=400)
        except Exception as exc:    # upstream model failure
            logger.exception('API summary generation failed')
            return json_error(f'Summary generation failed: {exc}', status=502)

        log_event(
            action='export', actor=request.user, request=request,
            changes={'format': 'ai_summary', 'count': count, 'selection': selection,
                     'output_tokens': result.output_tokens, 'truncated': result.truncated,
                     'source': 'api'},
        )
        if fmt == 'json':
            return JsonResponse({
                'count': count,
                'text': result.text,
                'truncated': result.truncated,
                'stop_reason': result.stop_reason,
                'input_tokens': result.input_tokens,
                'output_tokens': result.output_tokens,
                'max_tokens': result.max_tokens,
            })
        ctype = 'text/markdown' if fmt == 'md' else 'text/plain'
        resp = HttpResponse(result.text, content_type=f'{ctype}; charset=utf-8')
        ts = timezone.now().strftime('%Y%m%d_%H%M')
        resp['Content-Disposition'] = f'attachment; filename="scd_summary_{ts}.{fmt}"'
        return resp
