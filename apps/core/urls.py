from django.urls import path

from apps.entries.api_views import EntryCreateAPIView, EntryDetailAPIView
from apps.reports.api_views import ReportAPIView, ReportSummaryAPIView

from . import api_views, views

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('dashboard/summary/',              views.DashboardSummaryView.as_view(),            name='dashboard-summary'),
    path('dashboard/summary/download/txt/', views.DashboardSummaryDownloadTxtView.as_view(), name='dashboard-summary-txt'),
    path('dashboard/summary/download/md/',  views.DashboardSummaryDownloadMdView.as_view(),  name='dashboard-summary-md'),
    path('dashboard/summary/download/pdf/', views.DashboardSummaryDownloadPdfView.as_view(), name='dashboard-summary-pdf'),
    path('dashboard/prompt-config/',        views.DashboardPromptConfigView.as_view(),        name='dashboard-prompt-config'),
    path('about/', views.AboutView.as_view(), name='about'),
    path('api/',   views.ApiView.as_view(),   name='api'),
    path('mcp/',   views.McpView.as_view(),   name='mcp'),
    path('api/entries/', EntryCreateAPIView.as_view(), name='api-entry-create'),
    path('api/entries/<int:pk>/', EntryDetailAPIView.as_view(), name='api-entry-detail'),
    path('api/reports/',          ReportAPIView.as_view(),        name='api-reports'),
    path('api/reports/summary/',  ReportSummaryAPIView.as_view(), name='api-reports-summary'),
    path('api/whoami/',           api_views.WhoAmIAPIView.as_view(),  name='api-whoami'),
    path('api/taxonomy/',         api_views.TaxonomyAPIView.as_view(), name='api-taxonomy'),
    path('api/taxonomy/<slug:kind>/', api_views.TaxonomyAPIView.as_view(), name='api-taxonomy-kind'),
    path('bug-report/', views.BugReportView.as_view(), name='bug-report'),
    path('bug-report/submit/', views.BugReportSubmitView.as_view(), name='bug-report-submit'),
]
