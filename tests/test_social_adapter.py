"""Claim mapping in apps.accounts.adapters.SocialAccountAdapter."""

import pytest
from allauth.socialaccount.models import SocialAccount, SocialLogin

from apps.accounts.adapters import SocialAccountAdapter
from apps.accounts.models import User


OIDC_CLAIMS = {
    'sub': 'abc123',
    'email': 'rjones@fnal.gov',
    'preferred_username': 'rjones',
    'name': 'Robin Jones',
    'given_name': 'Robin',
    'family_name': 'Jones',
    'employee_number': 987654,
}


def _sociallogin(user, claims=OIDC_CLAIMS, nested=True):
    """Build a SocialLogin the way the provider would, post-lookup().

    The openid_connect provider nests claims under 'userinfo'; google stores
    them flat.
    """
    extra = {'userinfo': dict(claims)} if nested else dict(claims)
    account = SocialAccount(provider='keycloak', uid=claims['sub'], extra_data=extra)
    login = SocialLogin(account=account)
    login.user = user
    return login


@pytest.mark.django_db
class TestPreSocialLoginBackfill:
    def test_blank_fields_are_filled_from_claims(self):
        user = User.objects.create(username='rjones', email='rjones@fnal.gov')
        assert user.display_name == ''
        assert user.employee_id == ''

        SocialAccountAdapter().pre_social_login(None, _sociallogin(user))

        user.refresh_from_db()
        assert user.display_name == 'Robin Jones'
        assert user.employee_id == '987654'

    def test_existing_values_are_not_overwritten(self):
        user = User.objects.create(
            username='rjones',
            email='rjones@fnal.gov',
            display_name='Robin J. (locally edited)',
            employee_id='000001',
        )

        SocialAccountAdapter().pre_social_login(None, _sociallogin(user))

        user.refresh_from_db()
        assert user.display_name == 'Robin J. (locally edited)'
        assert user.employee_id == '000001'

    def test_one_blank_field_is_filled_while_the_other_is_kept(self):
        user = User.objects.create(
            username='rjones',
            email='rjones@fnal.gov',
            employee_id='000001',
        )

        SocialAccountAdapter().pre_social_login(None, _sociallogin(user))

        user.refresh_from_db()
        assert user.display_name == 'Robin Jones'
        assert user.employee_id == '000001'

    def test_flat_claims_from_google_are_also_read(self):
        user = User.objects.create(username='rjones', email='rjones@fnal.gov')

        SocialAccountAdapter().pre_social_login(
            None, _sociallogin(user, nested=False)
        )

        user.refresh_from_db()
        assert user.display_name == 'Robin Jones'
        assert user.employee_id == '987654'

    def test_missing_claims_leave_fields_blank(self):
        user = User.objects.create(username='rjones', email='rjones@fnal.gov')
        claims = {'sub': 'abc123', 'email': 'rjones@fnal.gov'}

        SocialAccountAdapter().pre_social_login(None, _sociallogin(user, claims))

        user.refresh_from_db()
        assert user.display_name == ''
        assert user.employee_id == ''

    def test_unsaved_user_is_skipped(self):
        """A first-time login has no DB record yet; populate_user() owns that."""
        user = User(username='rjones', email='rjones@fnal.gov')

        SocialAccountAdapter().pre_social_login(None, _sociallogin(user))

        assert user.pk is None
        assert user.display_name == ''


@pytest.mark.django_db
class TestPopulateUser:
    def test_nested_oidc_claims_reach_the_new_user(self):
        login = _sociallogin(User())
        user = SocialAccountAdapter().populate_user(
            None, login, {'email': 'rjones@fnal.gov', 'username': 'rjones'}
        )
        assert user.display_name == 'Robin Jones'
        assert user.employee_id == '987654'

    def test_display_name_falls_back_to_given_and_family_name(self):
        claims = dict(OIDC_CLAIMS)
        del claims['name']
        login = _sociallogin(User(), claims)
        user = SocialAccountAdapter().populate_user(None, login, {})
        assert user.display_name == 'Robin Jones'

    def test_display_name_falls_back_to_preferred_username(self):
        claims = {k: v for k, v in OIDC_CLAIMS.items()
                  if k not in ('name', 'given_name', 'family_name')}
        login = _sociallogin(User(), claims)
        user = SocialAccountAdapter().populate_user(None, login, {})
        assert user.display_name == 'rjones'
