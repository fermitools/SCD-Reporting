"""Bearer-token authentication shared by every /api/ endpoint.

The token API is deliberately token-only: a session cookie is never accepted,
because the views are CSRF-exempt and a cookie-authenticated, CSRF-exempt POST
(e.g. the AI summary, which costs money) would be forgeable from another site.
"""
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt


def json_error(message, status=400, **extra):
    body = {'error': message}
    body.update(extra)
    return JsonResponse(body, status=status)


def authenticate_token(request):
    """Return the active User owning the request's Bearer token, or None."""
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth.startswith('Bearer '):
        return None
    key = auth[len('Bearer '):].strip()
    if not key:
        return None
    from apps.accounts.models import APIToken
    try:
        token = APIToken.objects.select_related('user').get(key=key)
    except APIToken.DoesNotExist:
        return None
    if not token.user.is_active:
        return None
    token.last_used_at = timezone.now()
    token.save(update_fields=['last_used_at'])
    return token.user


def can_report(user):
    """Same predicate as AuditorOrAdminRequiredMixin (the Reports page)."""
    return bool(user.is_auditor or user.is_group_leader
                or user.is_division_head or user.is_functional_lead)


@method_decorator(csrf_exempt, name='dispatch')
class TokenAPIView(View):
    """Base view: authenticates the Bearer token and sets ``request.user``.

    Subclasses set ``require_reporter = True`` to restrict to the roles that can
    open the Reports page.
    """

    require_reporter = False

    def dispatch(self, request, *args, **kwargs):
        user = authenticate_token(request)
        if user is None:
            return json_error('Valid Bearer token required.', status=401)
        request.user = user
        if self.require_reporter and not can_report(user):
            return json_error(
                'Reports require the Auditor, Group Leader, Division Head, '
                'Functional Lead or Admin role.', status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def http_method_not_allowed(self, request, *args, **kwargs):
        allowed = [m.upper() for m in self.http_method_names if hasattr(self, m)]
        resp = json_error(f'Method {request.method} not allowed.', status=405)
        resp['Allow'] = ', '.join(allowed)
        return resp
