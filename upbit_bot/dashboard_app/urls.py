from django.urls import path
from . import views

urlpatterns = [
    path('', views.overview, name='overview'),
    path('trades/', views.trades, name='trades'),
    path('chart/', views.live_chart, name='chart'),
    # API endpoints (AJAX)
    path('api/overview/', views.api_overview, name='api_overview'),
    path('api/chart/<str:market>/<int:unit>/', views.api_chart, name='api_chart'),
    path('api/market-env/<str:market>/', views.api_market_env, name='api_market_env'),
    path('api/cumulative/', views.api_cumulative, name='api_cumulative'),
]
