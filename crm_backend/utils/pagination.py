"""Manual async pagination shared by list endpoints.

Not a generic pagination framework: Ninja's ``@paginate`` decorator targets
sync querysets and would fight the async view style used throughout this
API, so this is a small, explicit helper instead.
"""

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


async def paginate(queryset, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    """Return (items, meta) for one page of an async-iterable queryset."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await queryset.acount()
    offset = (page - 1) * page_size
    items = [item async for item in queryset[offset:offset + page_size]]
    meta = {'count': total, 'page': page, 'page_size': page_size, 'has_next': offset + page_size < total}
    return items, meta
