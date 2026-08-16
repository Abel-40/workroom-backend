"""Shared abstract model bases.

Not a registered Django app: this module only defines abstract models, which
never get their own migrations/table, so no INSTALLED_APPS entry is needed.
"""

import uuid

from django.db import models


class UUIDModel(models.Model):
    """Base for models that should use a UUID primary key instead of a
    sequential integer, so IDs never leak enumeration information (row
    counts, creation order) through the API."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True
