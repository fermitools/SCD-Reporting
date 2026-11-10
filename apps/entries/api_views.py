import json
from datetime import date

from django.db import transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.urls import reverse

from apps.core.api_auth import TokenAPIView, authenticate_token
from apps.core.api_params import ParamError, int_param, request_params, resolve_taxonomy
from apps.reports import exporters
from apps.taxonomy.models import Category, LabPriority, Project, Tag, WorkGroup

from .models import WorkItem


def _json_error(message, status=400):
    return JsonResponse({'error': message}, status=status)


# Kept for any external import of the old name; the shared helper also rejects
# tokens that belong to deactivated users.
_authenticate_token = authenticate_token


def _resolve_fk(model, value, allow_blank=False):
    """Resolve a slug or integer PK to a model instance. Returns (instance, error_str)."""
    if not value:
        if allow_blank:
            return None, None
        return None, f'{model.__name__} is required.'
    if str(value).isdigit():
        try:
            return model.objects.get(pk=int(value), is_active=True), None
        except model.DoesNotExist:
            pass
    try:
        return model.objects.get(slug=value, is_active=True), None
    except model.DoesNotExist:
        return None, f'{model.__name__} "{value}" not found or inactive.'


def _resolve_fk_list(model, value, required=True):
    """Resolve a slug/PK or list of them to a list of instances.

    Accepts a single value (backward-compatible) or a list. Returns
    (list_of_instances, error_str).
    """
    if value in (None, '', []):
        if required:
            return None, f'{model.__name__} is required.'
        return [], None
    values = value if isinstance(value, list) else [value]
    instances = []
    for v in values:
        inst, err = _resolve_fk(model, v)
        if err:
            return None, err
        instances.append(inst)
    return instances, None


def _get_multi(data, plural_key, singular_key):
    """Read a list field, preferring the plural key but accepting the legacy singular one."""
    if plural_key in data:
        return data[plural_key]
    return data.get(singular_key)


def _coerce_bool(val, default=False):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes')
    return bool(val) if val is not None else default


def _coerce_int(val, default=0, lo=0, hi=5):
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


class EntryCreateAPIView(TokenAPIView):
    """GET /api/entries/ — list the caller's entries; POST — create one."""

    def get(self, request):
        return entry_list(request)

    def post(self, request):
        user = request.user
        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            return _json_error('Request body must be valid JSON.')

        if not isinstance(data, dict):
            return _json_error('Request body must be a JSON object.')

        errors = {}

        title = str(data.get('title', '')).strip()
        if not title:
            errors['title'] = 'This field is required.'

        description = str(data.get('description', '')).strip()
        if not description:
            errors['description'] = 'This field is required.'

        period_kind = data.get('period_kind', 'week')
        if period_kind not in WorkItem.PeriodKind.values:
            errors['period_kind'] = f'Must be one of: {", ".join(WorkItem.PeriodKind.values)}.'

        raw_start = data.get('period_start')
        raw_end = data.get('period_end')
        parsed_start = parsed_end = None

        if not raw_start:
            errors['period_start'] = 'This field is required.'
        else:
            try:
                parsed_start = date.fromisoformat(str(raw_start))
            except ValueError:
                errors['period_start'] = 'Date must be YYYY-MM-DD.'

        if not raw_end:
            errors['period_end'] = 'This field is required.'
        else:
            try:
                parsed_end = date.fromisoformat(str(raw_end))
            except ValueError:
                errors['period_end'] = 'Date must be YYYY-MM-DD.'

        if parsed_start and parsed_end and parsed_end < parsed_start:
            errors['period_end'] = 'period_end must be on or after period_start.'

        projects, err = _resolve_fk_list(Project, _get_multi(data, 'projects', 'project'))
        if err:
            errors['projects'] = err

        categories, err = _resolve_fk_list(Category, _get_multi(data, 'categories', 'category'))
        if err:
            errors['categories'] = err

        group, err = _resolve_fk(WorkGroup, data.get('group'), allow_blank=True)
        if err:
            errors['group'] = err

        lab_priorities, err = _resolve_fk_list(
            LabPriority, _get_multi(data, 'lab_priorities', 'lab_priority'), required=False,
        )
        if err:
            errors['lab_priorities'] = err

        if errors:
            return JsonResponse({'errors': errors}, status=400)

        with transaction.atomic():
            entry = WorkItem.objects.create(
                author=user,
                title=title,
                description=description,
                group=group,
                period_kind=period_kind,
                period_start=parsed_start,
                period_end=parsed_end,
                is_private=_coerce_bool(data.get('is_private')),
                is_critical=_coerce_bool(data.get('is_critical')),
                is_highlight=_coerce_bool(data.get('is_highlight')),
                highlight_stars=_coerce_int(data.get('highlight_stars')),
                is_division_head_only=_coerce_bool(data.get('is_division_head_only')),
            )
            entry.projects.set(projects)
            entry.categories.set(categories)
            entry.lab_priorities.set(lab_priorities)

            tag_names = data.get('tags', [])
            if isinstance(tag_names, list):
                tags = []
                for name in tag_names:
                    name = str(name).strip().lower()
                    if name:
                        tag, created = Tag.objects.get_or_create(name=name)
                        Tag.objects.filter(pk=tag.pk).update(use_count=F('use_count') + 1)
                        tags.append(tag)
                if tags:
                    entry.tags.set(tags)

        project_slugs = [p.slug for p in projects]
        category_slugs = [c.slug for c in categories]
        return JsonResponse({
            'id': entry.pk,
            'title': entry.title,
            'projects': project_slugs,
            'categories': category_slugs,
            # Legacy single-value keys (first selected) for backward compatibility.
            'project': project_slugs[0] if project_slugs else None,
            'category': category_slugs[0] if category_slugs else None,
            'period_kind': entry.period_kind,
            'period_start': entry.period_start.isoformat(),
            'period_end': entry.period_end.isoformat(),
            'url': request.build_absolute_uri(reverse('entries:detail', kwargs={'pk': entry.pk})),
        }, status=201)


# ── Read API ──────────────────────────────────────────────────────────────────

ENTRY_LIST_MAX = 1000


def _ref(obj):
    return {'id': obj.pk, 'slug': obj.slug, 'name': obj.name} if obj else None


def serialize_entry(item, request):
    return {
        'id': item.pk,
        'title': item.title,
        'description': item.description,
        'author': {
            'email': item.author.email,
            'display_name': item.author.display_name or '',
        },
        'projects': [_ref(p) for p in item.projects.all()],
        'categories': [_ref(c) for c in item.categories.all()],
        'lab_priorities': [_ref(lp) for lp in item.lab_priorities.all()],
        'entry_type': _ref(item.entry_type),
        'group': _ref(item.group),
        'tags': sorted(t.name for t in item.tags.all()),
        'period_kind': item.period_kind,
        'period_start': item.period_start.isoformat(),
        'period_end': item.period_end.isoformat(),
        'is_private': item.is_private,
        'is_critical': item.is_critical,
        'is_highlight': item.is_highlight,
        'highlight_stars': item.highlight_stars,
        'is_division_head_only': item.is_division_head_only,
        'is_archived': item.is_archived,
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'url': request.build_absolute_uri(reverse('entries:detail', kwargs={'pk': item.pk})),
    }


def _entry_qs():
    return (WorkItem.objects
            .select_related('author', 'entry_type', 'group')
            .prefetch_related('projects', 'categories', 'lab_priorities', 'tags'))


def _date_param(params, key):
    raw = params.get(key, '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ParamError({key: 'Date must be YYYY-MM-DD.'})


def _export(request, qs, fmt, source):
    exporter = exporters.get(fmt)
    if exporter is None:
        raise ParamError({'format': f'Unknown format "{fmt}". '
                                    f'Available: json, {", ".join(exporters.available())}.'})
    from apps.audit.service import log_event
    log_event(action='export', actor=request.user, request=request,
              changes={'format': fmt, 'count': qs.count(), 'source': source})
    return exporter(qs)


def entry_list(request):
    """The caller's own entries (the "My Entries" scope), filtered and paged.

    ``format=json`` (default) returns one page; any exporter format returns the
    whole filtered set as a file, ignoring limit/offset.
    """
    try:
        params = request_params(request)
        for alias, canonical in (('project', 'projects'), ('category', 'categories')):
            if alias in params:
                params.setlist(canonical, params.getlist(canonical) + params.getlist(alias))
        resolve_taxonomy(params, {'projects': Project, 'categories': Category})
        after = _date_param(params, 'period_after')
        before = _date_param(params, 'period_before')
        limit = int_param(params, 'limit', 100, 1, ENTRY_LIST_MAX)
        offset = int_param(params, 'offset', 0, 0, 10 ** 9)
        archived = params.get('archived', '0').strip().lower()
        if archived not in ('0', '1', 'all', 'false', 'true'):
            raise ParamError({'archived': 'Must be 0, 1 or all.'})
        fmt = (params.get('format') or 'json').lower()

        qs = _entry_qs().filter(author=request.user)
        if archived in ('0', 'false'):
            qs = qs.filter(is_archived=False)
        elif archived in ('1', 'true'):
            qs = qs.filter(is_archived=True)
        distinct = False
        if params.getlist('projects'):
            qs = qs.filter(projects__pk__in=params.getlist('projects'))
            distinct = True
        if params.getlist('categories'):
            qs = qs.filter(categories__pk__in=params.getlist('categories'))
            distinct = True
        q = params.get('q', '').strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q)
                           | Q(tags__name__icontains=q))
            distinct = True
        # Overlap semantics: any entry whose period touches [after, before].
        if after:
            qs = qs.filter(period_end__gte=after)
        if before:
            qs = qs.filter(period_start__lte=before)
        if distinct:
            qs = qs.distinct()
        qs = qs.order_by('-period_end', '-pk')

        if fmt != 'json':
            return _export(request, qs, fmt, 'api-entries')
    except ParamError as exc:
        return JsonResponse({'errors': exc.errors}, status=400)

    total = qs.count()
    page = qs[offset:offset + limit]
    return JsonResponse({
        'count': total,
        'limit': limit,
        'offset': offset,
        'results': [serialize_entry(item, request) for item in page],
    })


def visible_entries(user):
    """Same visibility rule as the web entry detail page."""
    qs = _entry_qs()
    if user.is_scd_admin or user.is_division_head:
        return qs
    if user.is_auditor or user.is_group_leader or user.is_functional_lead:
        return qs.filter(Q(is_division_head_only=False) | Q(author=user))
    return qs.filter(author=user)


class EntryDetailAPIView(TokenAPIView):
    """GET /api/entries/<id>/ — one entry as JSON or any exporter format."""

    def get(self, request, pk):
        qs = visible_entries(request.user).filter(pk=pk)
        item = qs.first()
        if item is None:
            return JsonResponse({'error': f'Entry {pk} not found.'}, status=404)
        fmt = (request.GET.get('format') or 'json').lower()
        if fmt == 'json':
            return JsonResponse(serialize_entry(item, request))
        try:
            return _export(request, qs, fmt, 'api-entry')
        except ParamError as exc:
            return JsonResponse({'errors': exc.errors}, status=400)
