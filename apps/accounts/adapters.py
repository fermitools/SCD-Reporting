from django.conf import settings

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


def _claims(sociallogin):
    """Return the IdP claim set carried by a social login.

    The openid_connect provider stores extra_data nested as
    {'userinfo': {...}, 'id_token': {...}}; the google provider stores the
    decoded claims flat. Mirrors allauth's own _pick_data() — prefer userinfo,
    it generally carries more than the ID token.
    """
    extra = sociallogin.account.extra_data or {}
    return extra.get('userinfo') or extra.get('id_token') or extra


def _display_name_from(claims):
    """Display name: prefer full name, fall back to preferred_username."""
    return (
        claims.get('name')
        or f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
        or claims.get('preferred_username', '')
    )


def _employee_id_from(claims):
    """Employee ID: some IdPs expose this as employee_number or employeeNumber."""
    value = (
        claims.get('employee_number')
        or claims.get('employeeNumber')
        or claims.get('employee_id')
        or ''
    )
    return str(value) if value else ''


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        if getattr(settings, 'SCD_DISABLE_LOCAL_SIGNUP', False):
            return False
        from .models import SiteSettings
        return SiteSettings.get_solo().allow_signup


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        # SSO/social logins can always create accounts — only local signup
        # is gated by SiteSettings.allow_signup / SCD_DISABLE_LOCAL_SIGNUP.
        return True

    def populate_user(self, request, sociallogin, data):
        """Seed a *new* user from the standard OIDC claims (Keycloak / CILogon)."""
        user = super().populate_user(request, sociallogin, data)

        claims = _claims(sociallogin)

        display_name = _display_name_from(claims)
        if display_name:
            user.display_name = display_name

        employee_id = _employee_id_from(claims)
        if employee_id:
            user.employee_id = employee_id

        return user

    def pre_social_login(self, request, sociallogin):
        """Backfill claims onto an account that already exists.

        populate_user() only shapes the throwaway user instance built from the
        token; for a returning user SocialLogin.lookup() replaces it with the
        stored record, so nothing populate_user() set ever reaches the database
        after the first login. Fill in fields the IdP can supply that are still
        blank, but never overwrite a value already on the account — these are
        editable locally and a local edit wins.
        """
        super().pre_social_login(request, sociallogin)

        if not sociallogin.is_existing:
            return

        user = sociallogin.user
        claims = _claims(sociallogin)
        updated = []

        if not user.display_name:
            display_name = _display_name_from(claims)
            if display_name:
                user.display_name = display_name
                updated.append('display_name')

        if not user.employee_id:
            employee_id = _employee_id_from(claims)
            if employee_id:
                user.employee_id = employee_id
                updated.append('employee_id')

        if updated:
            user.save(update_fields=updated)
