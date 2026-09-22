# mhvtl_system/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Redirect root URL to login page for now
    path('', lambda request: redirect('authentication:login'), name='home'),
    
    # App URLs
    path('auth/', include('apps.authentication.urls')),
    
    # Temporarily comment out dashboard until we create it
    # path('dashboard/', include('apps.dashboard.urls')),
    
    # Add other apps as you develop them
    path('libraries/', include('apps.libraries.urls')),
    # path('media/', include('apps.media_management.urls')),
    # path('monitoring/', include('apps.monitoring.urls')),
    # path('iscsi/', include('apps.iscsi_targets.urls')),
    # path('testing/', include('apps.testing.urls')),
    # path('admin-panel/', include('apps.system_admin.urls')),
    # path('maintenance/', include('apps.maintenance.urls')),
    # path('api/', include('apps.api.urls')),
]

# Serve media and static files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Static files are automatically served by django.contrib.staticfiles app from STATICFILES_DIRS
    # No need to manually add static() for STATIC_URL in development

    # Add debug toolbar URLs if available
    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar
        urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns