from django.urls import path

from . import views

app_name = 'entries'

urlpatterns = [
    path('',              views.EntryListView.as_view(),   name='list'),
    path('export/',             views.EntryExportView.as_view(),            name='export'),
    path('new/',                views.EntryCreateView.as_view(),            name='create'),
    path('new/from-summary/',   views.EntryCreateFromSummaryView.as_view(), name='create-from-summary'),
    path('<int:pk>/',     views.EntryDetailView.as_view(), name='detail'),
    path('<int:pk>/edit/',   views.EntryUpdateView.as_view(), name='edit'),
    path('<int:pk>/delete/', views.EntryDeleteView.as_view(), name='delete'),
    path('period-prefill/',    views.PeriodPrefillView.as_view(),    name='period-prefill'),
    path('markdown-preview/',  views.MarkdownPreviewView.as_view(),  name='markdown-preview'),
    path('template-save/',     views.EntryTemplateSaveView.as_view(),   name='template-save'),
    path('template-delete/',   views.EntryTemplateDeleteView.as_view(), name='template-delete'),
    path('manage/',                       views.EntryManageView.as_view(),       name='manage'),
    path('<int:pk>/reassign/',            views.EntryReassignView.as_view(),     name='reassign'),
    path('<int:pk>/archive/',             views.EntryArchiveView.as_view(),      name='archive'),
    path('<int:pk>/manager-delete/',      views.EntryManagerDeleteView.as_view(), name='manager-delete'),
]
