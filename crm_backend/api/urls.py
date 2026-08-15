"""Compatibility URL module; the active API is registered in ``api.api``."""

from django.urls import path

from .api import api

urlpatterns = [path('', api.urls)]
