from django.apps import AppConfig


class PermissionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'permissions'

    def ready(self):
        # Fail fast on a malformed/drifted RBAC catalog at process boot,
        # rather than on the first authorization check in production.
        from . import catalog
        catalog.load_catalog()
