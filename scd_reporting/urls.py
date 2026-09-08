from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('', include('apps.core.urls')),
    path('', include('apps.accounts.urls')),
    path('taxonomy/', include('apps.taxonomy.urls', namespace='taxonomy')),
    path('entries/',  include('apps.entries.urls',  namespace='entries')),
    path('reports/',  include('apps.reports.urls',  namespace='reports')),
    path('audit/',    include('apps.audit.urls',    namespace='audit')),
    path('reminders/', include('apps.reminders.urls', namespace='reminders')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
