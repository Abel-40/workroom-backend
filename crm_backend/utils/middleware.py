"""Request-ID propagation (Phase 11: structured logs / request IDs).

Every request gets a correlation id -- reused from an inbound X-Request-ID
header if the caller/proxy already set one, otherwise generated here -- so a
single request's log lines can be correlated with each other and with the
id echoed back in the response header. Celery tasks run in a separate
process and use their own correlation keys (e.g. generation_id) instead.
"""

import uuid
from contextvars import ContextVar

_request_id_ctx: ContextVar[str] = ContextVar('request_id', default='-')


def get_current_request_id() -> str:
    return _request_id_ctx.get()


class RequestIDMiddleware:
    """Fully async middleware -- the app is ASGI end-to-end (see api/api.py),
    so this must not force a sync/async adaptation."""

    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        request_id = request.headers.get('X-Request-ID') or uuid.uuid4().hex
        token = _request_id_ctx.set(request_id)
        try:
            response = await self.get_response(request)
        finally:
            _request_id_ctx.reset(token)
        response['X-Request-ID'] = request_id
        return response
