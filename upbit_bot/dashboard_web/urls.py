from django.urls import path, include

urlpatterns = [
    path('', include('dashboard_app.urls')),
]
