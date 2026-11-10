from django.shortcuts import redirect
from django.urls import reverse

_SELECT_GROUP_URL = None  # resolved lazily to avoid AppRegistryNotReady


def _select_group_url():
    global _SELECT_GROUP_URL
    if _SELECT_GROUP_URL is None:
        _SELECT_GROUP_URL = reverse('select-group')
    return _SELECT_GROUP_URL


# Paths that must always be reachable regardless of group/role status
_PASSTHROUGH_PREFIXES = (
    '/accounts/',   # allauth login/logout/SSO callbacks
    '/api/',        # token API: JSON clients must never be redirected to an HTML page
    '/static/',
    '/media/',
    '/__debug__/',
)


class RolePreviewMiddleware:
    """
    Lets admins temporarily preview the UI as another role.

    Reads ``_preview_role`` from the session. If set, overrides
    ``request.user.role`` for this request only — the database is never
    modified. Sets ``request.user._is_previewing = True`` so templates can
    show the preview banner.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            from apps.accounts.models import User
            # Check the *real* database role before any override
            if request.user.role == User.Role.ADMIN:
                preview_role = request.session.get('_preview_role')
                if preview_role and preview_role in User.Role.values and preview_role != User.Role.ADMIN:
                    request.user._is_previewing = True
                    request.user._preview_label = dict(User.Role.choices).get(preview_role, preview_role)
                    request.user.role = preview_role

        return self.get_response(request)


class GroupSelectionMiddleware:
    """Redirect authenticated users without a group to the group selection page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.user.group_id
            and not getattr(request.user, '_is_previewing', False)
            and not request.session.get('_group_selection_done')
            and not self._is_passthrough(request.path)
            and request.path != _select_group_url()
        ):
            from apps.taxonomy.models import WorkGroup
            if WorkGroup.objects.filter(is_active=True).exists():
                return redirect(_select_group_url())

        return self.get_response(request)

    @staticmethod
    def _is_passthrough(path):
        return any(path.startswith(p) for p in _PASSTHROUGH_PREFIXES)
