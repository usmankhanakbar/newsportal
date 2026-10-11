from django.urls import path
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('dawn/', views.dawn_view, name='dawn'),
    path('thenews/', views.thenews_view, name='thenews'),
    path('thenation/', views.thenation_view, name='thenation'),
    path('dailytimes/', views.dailytimes_view, name='dailytimes'),
    path('tribune/', views.tribune_view, name='tribune'),

    # SEO
    path('sitemap.xml', views.sitemap_xml, name='sitemap'),
    path('robots.txt', views.robots_txt, name='robots'),
    path('ads.txt', views.ads_txt, name='ads_txt'),
    path('article/', views.article_detail, name='article_detail'),
    path('refresh/', views.force_refresh, name='force_refresh'),

    # Daily News full-list JSON API
    path('api/daily/<str:paper>/', views.daily_paper_json, name='daily_paper_json'),
]