"""Production settings. Applied whenever DEBUG=False (see __init__.py in
this package). This hardening block is unchanged from the original single
settings.py file's `if not DEBUG: ...` branch -- just moved from a runtime
conditional into module selection, so the computed settings are identical
for any given DEBUG value.

Full deployment hardening (rate limiting, HSTS tuning, reverse proxy
headers, etc.) beyond what's here is completed in Phase 11.
"""

from .base import *  # noqa: F401,F403

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 7
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
