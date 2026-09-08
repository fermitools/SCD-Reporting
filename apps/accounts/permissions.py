from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_scd_admin

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class AuditorOrAdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        return u.is_auditor or u.is_group_leader or u.is_division_head or u.is_functional_lead

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class EntryManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Admin, Division Head, or Group Leader — may reassign, archive, or delete any entry."""

    def test_func(self):
        u = self.request.user
        return u.is_scd_admin or u.is_division_head or u.is_group_leader

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class UserPageRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Admin, Division Head, or Group Leader — may view the Users page and edit primary groups."""

    def test_func(self):
        u = self.request.user
        return u.is_scd_admin or u.is_division_head or u.is_group_leader

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class TaxonomyEditorRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Admin, Division Head, or Group Leader — may create and edit taxonomy items."""

    def test_func(self):
        u = self.request.user
        return u.is_scd_admin or u.is_division_head or u.is_group_leader

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class ReminderSenderRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Functional Lead and above — may view the reminder pages and send reminders.

    Auditor is excluded on purpose: it is a read-only role, and mailing people
    on the division's behalf is a write action.
    """

    def test_func(self):
        from apps.reminders.scope import can_send
        return can_send(self.request.user)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied
