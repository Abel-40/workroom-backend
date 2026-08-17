"""Minimal async rate limiting for abuse-prone auth endpoints (Phase 11).

Fixed-window counter (INCR + EXPIRE) against the same Redis instance Celery
already uses (settings.CELERY_BROKER_URL) -- no new dependency. Not meant as
a general-purpose throttling framework, just a guard on signup/signin/invite
endpoints against brute-force and spam.
"""

import asyncio
import functools
import logging

import redis.asyncio as aioredis
from django.conf import settings
from ninja.errors import HttpError

logger = logging.getLogger(__name__)

_redis_client: aioredis.Redis | None = None
_redis_client_loop: asyncio.AbstractEventLoop | None = None


def _client() -> aioredis.Redis:
    """A client's connections are bound to the event loop that created it.
    Django's async test client (and asgiref generally) can run different
    requests on different event loops, so a naively-cached module-level
    client breaks the second time it's used under a new loop ("attached to
    a different loop" / "Event loop is closed"). Recreate the client
    whenever the running loop has changed."""
    global _redis_client, _redis_client_loop
    current_loop = asyncio.get_running_loop()
    if _redis_client is None or _redis_client_loop is not current_loop:
        _redis_client = aioredis.from_url(settings.CELERY_BROKER_URL)
        _redis_client_loop = current_loop
    return _redis_client


def _default_key(request) -> str:
    return request.META.get('REMOTE_ADDR', 'unknown')


def rate_limit(name: str, limit: int, window_seconds: int, key_func=_default_key):
    """Decorator for an async Ninja view function. Raises HttpError(429) once
    `limit` calls happen within `window_seconds` for the same key.

    Disabled entirely via settings.RATE_LIMIT_ENABLED (off in tests -- see
    conftest.py -- so the suite never depends on a real Redis and never goes
    flaky from counters accumulating across test runs). Fails open (logs and
    lets the request through) if Redis itself is unreachable, since this is
    a defense-in-depth guard, not the endpoint's primary security control.
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        async def wrapped(request, *args, **kwargs):
            if getattr(settings, 'RATE_LIMIT_ENABLED', True):
                key = f'ratelimit:{name}:{key_func(request)}'
                try:
                    client = _client()
                    current = await client.incr(key)
                    if current == 1:
                        await client.expire(key, window_seconds)
                except HttpError:
                    raise
                except Exception:
                    # 'name' is a reserved LogRecord attribute (the logger's
                    # own name) -- passing it in `extra` raises a KeyError
                    # from logging internals, so use a non-colliding key.
                    logger.warning('rate_limit.backend_unreachable', extra={'limit_name': name})
                else:
                    if current > limit:
                        raise HttpError(429, 'Too many requests. Please try again later.')
            return await view_func(request, *args, **kwargs)

        return wrapped

    return decorator
