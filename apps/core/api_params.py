"""Request-parameter helpers for the token API.

Endpoints accept parameters from the query string, a form-encoded body or a
JSON object body; all three are merged into one QueryDict so that the
django-filter FilterSets used by the web UI can be reused unchanged.
"""
import json

from django.http import QueryDict

TRUE_STRINGS = ('1', 'true', 'yes', 'on')


class ParamError(ValueError):
    """Bad request parameters; ``errors`` maps field name to message."""

    def __init__(self, errors):
        super().__init__('; '.join(f'{k}: {v}' for k, v in errors.items()))
        self.errors = errors


def _scalar(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return '' if v is None else str(v)


def request_params(request):
    """Return a mutable QueryDict of GET params overlaid with the body params."""
    params = request.GET.copy()
    if request.method != 'POST':
        return params
    ctype = request.META.get('CONTENT_TYPE', '')
    if ctype.startswith('application/json'):
        if not request.body:
            return params
        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            raise ParamError({'body': 'Request body must be valid JSON.'})
        if not isinstance(data, dict):
            raise ParamError({'body': 'Request body must be a JSON object.'})
        for key, value in data.items():
            values = value if isinstance(value, list) else [value]
            params.setlist(key, [_scalar(v) for v in values])
    else:
        for key in request.POST:
            params.setlist(key, request.POST.getlist(key))
    return params


def is_true(params, key):
    return params.get(key, '').strip().lower() in TRUE_STRINGS


def resolve_taxonomy(params, fields):
    """Replace slug values with primary keys, in place.

    ``fields`` maps a parameter name to its taxonomy model. Numeric values are
    passed through untouched (the FilterSet validates them). Comma-separated
    values are split so ``projects=dune,nova`` works as well as repeating the
    parameter. An unknown slug raises ParamError rather than silently matching
    nothing.
    """
    errors = {}
    for name, model in fields.items():
        raw = params.getlist(name)
        if not raw:
            continue
        values = [v.strip() for item in raw for v in item.split(',') if v.strip()]
        resolved = []
        for v in values:
            if v.isdigit():
                resolved.append(v)
                continue
            pk = model.objects.filter(slug=v).values_list('pk', flat=True).first()
            if pk is None:
                errors[name] = f'{model.__name__} "{v}" not found.'
                break
            resolved.append(str(pk))
        params.setlist(name, resolved)
    if errors:
        raise ParamError(errors)
    return params


def int_param(params, key, default, lo, hi):
    raw = params.get(key, '')
    if raw == '':
        return default
    try:
        n = int(raw)
    except ValueError:
        raise ParamError({key: 'Must be an integer.'})
    if not lo <= n <= hi:
        raise ParamError({key: f'Must be between {lo} and {hi}.'})
    return n


def id_list(params, key):
    out = []
    for item in params.getlist(key):
        for v in item.split(','):
            v = v.strip()
            if not v:
                continue
            if not v.isdigit():
                raise ParamError({key: f'"{v}" is not an integer id.'})
            out.append(int(v))
    return out
