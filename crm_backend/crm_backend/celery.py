"""Celery application for background jobs (AI generation, email, etc.).

Django remains the source of truth; Celery/Redis are transport only (see
DEVELOPMENT_RULES Rule 5). Task modules live in each owning app as
``<app>/tasks.py`` and are auto-discovered below.
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crm_backend.settings')

app = Celery('crm_backend')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
