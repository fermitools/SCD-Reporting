from django import forms
from django.db.models import F

from apps.taxonomy.models import Category, EntryType, LabPriority, Project, Tag, WorkGroup

from .models import WorkItem


class WorkItemForm(forms.ModelForm):
    tags_input = forms.CharField(required=False, widget=forms.HiddenInput(), label='')

    class Meta:
        model = WorkItem
        fields = [
            'title', 'projects', 'categories', 'lab_priorities', 'entry_type', 'group',
            'period_kind', 'period_start', 'period_end',
            'description', 'is_private', 'is_critical', 'is_highlight', 'highlight_stars',
            'is_division_head_only',
        ]
        widgets = {
            'period_kind':    forms.HiddenInput(),
            'period_start':   forms.DateInput(attrs={'type': 'date'}),
            'period_end':     forms.DateInput(attrs={'type': 'date'}),
            'description':    forms.Textarea(attrs={'rows': 8}),
            'highlight_stars': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['projects'].queryset   = Project.objects.filter(is_active=True).order_by('sort_order', 'name')
        self.fields['categories'].queryset = Category.objects.filter(is_active=True).order_by('sort_order', 'name')
        # At least one project and one category remain required (ModelMultipleChoiceField
        # is required by default — an empty multi-select fails validation).
        self.fields['lab_priorities'].queryset = LabPriority.objects.filter(is_active=True).order_by('sort_order', 'name')
        self.fields['lab_priorities'].required = False
        self.fields['entry_type'].queryset = EntryType.objects.filter(is_active=True).order_by('sort_order', 'name')
        self.fields['entry_type'].required = False
        self.fields['group'].queryset      = WorkGroup.objects.filter(is_active=True).order_by('sort_order', 'name')
        self.fields['group'].required      = False
        self.fields['highlight_stars'].required = False

        # The period inputs are rendered by a shared partial that echoes the value
        # straight back into <input type="date">. Edit mode seeds these initials
        # from the instance as date objects while create mode and re-rendered
        # (bound) forms carry ISO strings, so normalise to ISO strings here and let
        # the template pass the value through untouched (GitHub #28: piping it
        # through the |date filter blanked the strings on validation errors).
        for _name in ('period_start', 'period_end'):
            _val = self.initial.get(_name)
            if hasattr(_val, 'isoformat'):
                self.initial[_name] = _val.isoformat()

        # Style the multi-select widgets to match the rest of the form.
        _multi_cls = ('w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm '
                      'focus:border-scd-primary focus:ring-1 focus:ring-scd-primary focus:outline-none')
        for _name in ('projects', 'categories', 'lab_priorities'):
            self.fields[_name].widget.attrs.update({'class': _multi_cls, 'size': 5})
        if self.instance.pk:
            self.fields['tags_input'].initial = ','.join(
                self.instance.tags.values_list('name', flat=True)
            )

    def clean_highlight_stars(self):
        val = self.cleaned_data.get('highlight_stars')
        return val if val is not None else 0

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('period_start')
        end   = cleaned.get('period_end')
        if start and end and end < start:
            raise forms.ValidationError('Period end must be on or after period start.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            self._sync_tags(instance)
        return instance

    def _sync_tags(self, instance):
        raw = self.cleaned_data.get('tags_input', '')
        new_names = {t.strip().lower() for t in raw.split(',') if t.strip()}

        old_ids = set(instance.tags.values_list('id', flat=True))

        new_tags = []
        for name in new_names:
            tag, _ = Tag.objects.get_or_create(name=name)
            new_tags.append(tag)
        new_ids = {t.id for t in new_tags}

        added   = new_ids - old_ids
        removed = old_ids - new_ids
        if added:
            Tag.objects.filter(id__in=added).update(use_count=F('use_count') + 1)
        if removed:
            Tag.objects.filter(id__in=removed).update(use_count=F('use_count') - 1)

        instance.tags.set(new_tags)
