from django.urls import path

from . import views

app_name = 'reports'

urlpatterns = [
    path('',                      views.ReportIndexView.as_view(),         name='index'),
    path('preview/',              views.ReportPreviewView.as_view(),        name='preview'),
    path('download/<str:fmt>/',   views.ReportDownloadView.as_view(),       name='download'),
    path('summary/',              views.ReportSummaryView.as_view(),        name='summary'),
    path('summary/stream/',       views.ReportSummaryStreamView.as_view(),  name='summary-stream'),
    path('summary/download/txt/', views.SummaryDownloadTxtView.as_view(),   name='summary-download-txt'),
    path('summary/download/md/',  views.SummaryDownloadMdView.as_view(),    name='summary-download-md'),
    path('summary/download/pdf/', views.SummaryDownloadPdfView.as_view(),   name='summary-download-pdf'),
    path('prompt-config/',                  views.AIPromptConfigView.as_view(),     name='prompt-config'),
    path('prompt-template/<int:pk>/load/',         views.PromptTemplateLoadView.as_view(),        name='prompt-template-load'),
    path('prompt-template/<int:pk>/delete/',       views.PromptTemplateDeleteView.as_view(),      name='prompt-template-delete'),
    path('prompt-templates/admin/',                views.PromptTemplateAdminView.as_view(),        name='prompt-templates-admin'),
    path('prompt-template/<int:pk>/admin-delete/', views.PromptTemplateAdminDeleteView.as_view(), name='prompt-template-admin-delete'),
]
