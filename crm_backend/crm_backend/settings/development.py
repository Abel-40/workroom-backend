"""Development settings -- everything is inherited from base.py unmodified.
No production-only hardening (see production.py) is applied here, matching
the original single-file settings.py's behavior when DEBUG=True."""

from .base import *  # noqa: F401,F403
