"""Environment-selected settings package.

crm_backend.settings still resolves exactly the way it always did --
DJANGO_SETTINGS_MODULE=crm_backend.settings needs no change anywhere
(manage.py, wsgi.py, asgi.py, celery.py, pytest.ini, or any deployment
config), because Python treats this package's __init__.py as the module
when a dotted path names a package.

Selection uses the same DEBUG env var base.py already reads -- this is a
structural split (IMPLEMENTATION_PLAN.md Phase 0's "establish production
settings separation"), not a behavior change: for any given DEBUG value, the
computed settings are identical to what the single settings.py file this
package replaces used to produce.
"""

from .base import DEBUG

if DEBUG:
    from .development import *  # noqa: F401,F403
else:
    from .production import *  # noqa: F401,F403
