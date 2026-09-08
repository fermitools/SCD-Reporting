from django import forms
from django.conf import settings

from .models import ReminderSchedule, ReminderTemplate

_INPUT = ('w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm '
          'focus:border-scd-primary focus:ring-1 focus:ring-scd-primary focus:outline-none')


def _common_timezones():
    """A short, ordered list of plausible zones, with the site default first."""
    zones = [settings.TIME_ZONE, 'UTC', 'America/Chicago', 'America/New_York',
             'America/Denver', 'America/Los_Angeles', 'Europe/London',
             'Europe/Zurich', 'Europe/Geneva', 'Asia/Tokyo']
    seen, ordered = set(), []
    for z in zones:
        if z not in seen:
            seen.add(z)
            ordered.append(z)
    return [(z, z) for z in ordered]


class ReminderTemplateForm(forms.ModelForm):
    class Meta:
        model = ReminderTemplate
        fields = ['name', 'subject', 'body', 'is_default']
        widgets = {
            'name':    forms.TextInput(attrs={'class': _INPUT}),
            'subject': forms.TextInput(attrs={'class': _INPUT}),
            'body':    forms.Textarea(attrs={'class': _INPUT + ' font-mono', 'rows': 18}),
        }
        labels = {
            'is_default': 'Use this template when none is chosen',
        }


class ReminderScheduleForm(forms.ModelForm):
    timezone = forms.ChoiceField(
        choices=_common_timezones,
        widget=forms.Select(attrs={'class': _INPUT}),
        help_text='IANA timezone the send time is expressed in.',
    )

    class Meta:
        model = ReminderSchedule
        fields = [
            'name', 'template', 'frequency', 'weekday', 'day_of_month',
            'send_time', 'timezone', 'only_stale', 'stale_days', 'is_enabled',
        ]
        widgets = {
            'name':         forms.TextInput(attrs={'class': _INPUT}),
            'template':     forms.Select(attrs={'class': _INPUT}),
            'frequency':    forms.Select(attrs={'class': _INPUT}),
            'weekday':      forms.Select(attrs={'class': _INPUT}),
            'day_of_month': forms.NumberInput(attrs={'class': _INPUT, 'min': 1, 'max': 31}),
            'send_time':    forms.TimeInput(attrs={'class': _INPUT, 'type': 'time'}, format='%H:%M'),
            'stale_days':   forms.NumberInput(attrs={'class': _INPUT, 'min': 0, 'max': 365}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['template'].queryset = ReminderTemplate.objects.all()
        self.fields['stale_days'].initial = settings.REMINDER_STALE_DAYS
        if not self.instance.pk:
            self.fields['send_time'].initial = '15:00'
            self.fields['timezone'].initial = settings.TIME_ZONE

    def clean_day_of_month(self):
        value = self.cleaned_data.get('day_of_month') or 1
        if not 1 <= value <= 31:
            raise forms.ValidationError('Day of month must be between 1 and 31.')
        return value

    def clean_timezone(self):
        name = self.cleaned_data['timezone']
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise forms.ValidationError(f'Unknown timezone: {name}') from exc
        return name

    def save(self, commit=True):
        instance = super().save(commit=False)
        # Any change to the firing rule invalidates the stored claim token, so
        # recompute it from the new rule rather than firing on the old one.
        if not instance.pk or self._rule_changed():
            instance.next_run_at = None
        if commit:
            instance.save()
        return instance

    def _rule_changed(self):
        rule_fields = ('frequency', 'weekday', 'day_of_month', 'send_time', 'timezone')
        return any(f in self.changed_data for f in rule_fields)
