from django.urls import path

from . import views

app_name = 'reminders'

urlpatterns = [
    path('',      views.RecipientListView.as_view(), name='recipients'),
    path('send/', views.SendReminderView.as_view(),  name='send'),

    path('templates/',                  views.TemplateListView.as_view(),    name='templates'),
    path('templates/new/',              views.TemplateEditView.as_view(),    name='template-create'),
    path('templates/<int:pk>/edit/',    views.TemplateEditView.as_view(),    name='template-edit'),
    path('templates/<int:pk>/delete/',  views.TemplateDeleteView.as_view(),  name='template-delete'),
    path('templates/<int:pk>/default/', views.TemplateDefaultView.as_view(), name='template-default'),
    path('templates/preview/',          views.TemplatePreviewView.as_view(), name='template-preview'),

    path('schedules/',                 views.ScheduleListView.as_view(),   name='schedules'),
    path('schedules/new/',             views.ScheduleEditView.as_view(),   name='schedule-create'),
    path('schedules/<int:pk>/edit/',   views.ScheduleEditView.as_view(),   name='schedule-edit'),
    path('schedules/<int:pk>/delete/', views.ScheduleDeleteView.as_view(), name='schedule-delete'),
    path('schedules/<int:pk>/toggle/', views.ScheduleToggleView.as_view(), name='schedule-toggle'),
    path('schedules/<int:pk>/run/',    views.ScheduleRunNowView.as_view(), name='schedule-run'),

    path('log/', views.LogView.as_view(), name='log'),
]
