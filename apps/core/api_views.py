"""Token API discovery endpoints: who am I, and the taxonomy lookup tables.

GET /api/whoami/            — the token's user, role, scopes and capabilities
GET /api/taxonomy/          — every taxonomy table
GET /api/taxonomy/<kind>/   — one table (projects, categories, entry-types,
                              groups, lab-priorities, tags)
"""
from django.http import JsonResponse

from apps.core.api_auth import TokenAPIView, can_report, json_error
from apps.core.api_params import is_true
from apps.taxonomy.models import Category, EntryType, LabPriority, Project, Tag, WorkGroup

TAXONOMY_KINDS = {
    'projects':        Project,
    'categories':      Category,
    'entry-types':     EntryType,
    'groups':          WorkGroup,
    'lab-priorities':  LabPriority,
}


def _ref(obj):
    return {'id': obj.pk, 'slug': obj.slug, 'name': obj.name} if obj else None


class WhoAmIAPIView(TokenAPIView):
    def get(self, request):
        from apps.reports import exporters
        from apps.reports.views import _get_group_scope, _get_project_scope
        u = request.user
        group_scope = _get_group_scope(u)
        project_scope = _get_project_scope(u)
        token = getattr(u, 'api_token', None)
        return JsonResponse({
            'id': u.pk,
            'username': u.username,
            'email': u.email,
            'display_name': u.display_name or '',
            'role': u.role,
            'role_label': u.get_role_display(),
            'group': _ref(u.group),
            'can_report': can_report(u),
            # null means unrestricted; a list means reports are limited to it.
            'report_group_scope': None if group_scope is None else [_ref(g) for g in group_scope],
            'report_project_scope': None if project_scope is None else [_ref(p) for p in project_scope],
            'export_formats': ['json'] + [f for f in exporters.available() if f != 'json'],
            'token': {
                'created_at': token.created_at.isoformat() if token else None,
                'last_used_at': token.last_used_at.isoformat() if token and token.last_used_at else None,
            },
        })


def _taxonomy_rows(model, include_inactive):
    qs = model.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return [
        {'id': o.pk, 'slug': o.slug, 'name': o.name, 'short_code': o.short_code,
         'is_active': o.is_active, 'sort_order': o.sort_order}
        for o in qs.order_by('sort_order', 'name')
    ]


def _tag_rows():
    return [{'id': t.pk, 'name': t.name, 'use_count': t.use_count}
            for t in Tag.objects.order_by('-use_count', 'name')]


class TaxonomyAPIView(TokenAPIView):
    def get(self, request, kind=None):
        include_inactive = is_true(request.GET, 'all')
        if kind is None:
            data = {k: _taxonomy_rows(m, include_inactive) for k, m in TAXONOMY_KINDS.items()}
            data['tags'] = _tag_rows()
            return JsonResponse(data)
        if kind == 'tags':
            return JsonResponse({'tags': _tag_rows()})
        model = TAXONOMY_KINDS.get(kind)
        if model is None:
            return json_error(f'Unknown taxonomy kind "{kind}".', status=404,
                              kinds=sorted(list(TAXONOMY_KINDS) + ['tags']))
        return JsonResponse({kind: _taxonomy_rows(model, include_inactive)})
