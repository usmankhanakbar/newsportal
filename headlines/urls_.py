from django.urls import path
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('thenews/', views.thenews_view, name='thenews'),
    path('thenation/', views.thenation_view, name='thenation'),
    path('dailytimes/', views.dailytimes_view, name='dailytimes'),
    path('tribune/', views.tribune_view, name='tribune'),
]