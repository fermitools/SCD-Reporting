import shlex

import django_filters
from django.db.models import Q

from apps.entries.models import WorkItem
from apps.taxonomy.models import Category, EntryType, LabPriority, Project, WorkGroup

# How the period_after/period_before window is compared against an entry's own
# period (GitHub #30).
DATE_MATCH_CONTAINED = 'contained'
DATE_MATCH_OVERLAP   = 'overlap'
DATE_MATCH_CHOICES = [
    (DATE_MATCH_CONTAINED, 'Entirely within the range'),
    (DATE_MATCH_OVERLAP,   'Overlapping the range'),
]


class WorkItemFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(
        method='filter_search',
        label='Search',
    )
    author_email = django_filters.CharFilter(
        field_name='author__email',
        lookup_expr='icontains',
        label='Author email',
    )
    employee_group = django_filters.ModelChoiceFilter(
        field_name='author__group',
        queryset=WorkGroup.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Employee Group',
        empty_label='All groups',
    )
    group = django_filters.ModelChoiceFilter(
        queryset=WorkGroup.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Activity Group',
        empty_label='All groups',
    )
    projects = django_filters.ModelMultipleChoiceFilter(
        field_name='projects',
        queryset=Project.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Project',
        conjoined=False,  # match ANY of the selected projects
    )
    categories = django_filters.ModelMultipleChoiceFilter(
        field_name='categories',
        queryset=Category.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Category',
        conjoined=False,
    )
    entry_type = django_filters.ModelChoiceFilter(
        queryset=EntryType.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Entry Type',
        empty_label='All entry types',
    )
    lab_priorities = django_filters.ModelMultipleChoiceFilter(
        field_name='lab_priorities',
        queryset=LabPriority.objects.filter(is_active=True).order_by('sort_order', 'name'),
        label='Lab Priority',
        conjoined=False,
    )
    date_match = django_filters.ChoiceFilter(
        choices=DATE_MATCH_CHOICES,
        method='filter_noop',
        label='Match entries',
        empty_label=None,
    )
    period_after = django_filters.DateFilter(
        method='filter_period_after',
        label='Range start',
    )
    period_before = django_filters.DateFilter(
        method='filter_period_before',
        label='Range end',
    )
    is_private      = django_filters.BooleanFilter(label='Private only')
    exclude_private = django_filters.BooleanFilter(
        method='filter_exclude_private',
        label='Exclude private',
    )

    def filter_search(self, queryset, name, value):
        try:
            tokens = shlex.split(value)
        except ValueError:
            tokens = value.split()

        OPERATORS = {'AND', 'OR', 'XOR'}

        # Normalise token list: insert default OR between consecutive non-operator tokens.
        # Result is always [term, OP, term, OP, ...].
        normalized = []
        prev_was_term = False
        for token in tokens:
            if token.upper() in OPERATORS:
                if prev_was_term:
                    normalized.append(token.upper())
                    prev_was_term = False
            else:
                if prev_was_term:
                    normalized.append('OR')
                normalized.append(token)
                prev_was_term = True

        if not normalized:
            return queryset

        def term_q(t):
            return Q(title__icontains=t) | Q(description__icontains=t)

        result = term_q(normalized[0])
        i = 1
        while i < len(normalized) - 1:
            op = normalized[i]
            next_q = term_q(normalized[i + 1])
            if op == 'AND':
                result = result & next_q
            elif op == 'XOR':
                result = (result | next_q) & ~(result & next_q)
            else:  # OR
                result = result | next_q
            i += 2

        return queryset.filter(result)

    def _overlap_mode(self):
        return (self.data or {}).get('date_match') == DATE_MATCH_OVERLAP

    def filter_period_after(self, queryset, name, value):
        """Lower bound of the requested window.

        Containment mode (the default) keeps entries that start no earlier than
        the bound. Overlap mode keeps any entry that has not already finished by
        then, i.e. period_end >= bound.
        """
        if self._overlap_mode():
            return queryset.filter(period_end__gte=value)
        return queryset.filter(period_start__gte=value)

    def filter_period_before(self, queryset, name, value):
        """Upper bound of the requested window.

        Containment mode keeps entries that end no later than the bound. Overlap
        mode keeps any entry that had already started by then, i.e.
        period_start <= bound.
        """
        if self._overlap_mode():
            return queryset.filter(period_start__lte=value)
        return queryset.filter(period_end__lte=value)

    def filter_noop(self, queryset, name, value):
        """date_match selects how the other date filters behave; it filters nothing itself."""
        return queryset

    def filter_exclude_private(self, queryset, name, value):
        if value:
            return queryset.filter(is_private=False)
        return queryset

    class Meta:
        model = WorkItem
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Use plain DateInput for date fields
        import django.forms as forms
        for fname in ('period_after', 'period_before'):
            self.filters[fname].field.widget = forms.DateInput(attrs={'type': 'date'})
