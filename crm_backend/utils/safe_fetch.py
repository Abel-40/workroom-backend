"""SSRF-safe fetch of a user-supplied URL, for the AI project assistant's
"read this link" capability (ai_agent/tasks_assistant.py).

Runs inside the Celery task (off the request thread) since it's unbounded
network I/O to an address chosen by the user, same reason the AI service
call itself never happens synchronously in a Django view.

No new dependency: HTML-to-text uses stdlib html.parser, not BeautifulSoup
-- adequate for extracting readable text from a page for LLM context, and
CLAUDE.md says not to add a dependency the existing stack can solve cleanly.

Known accepted gap: the DNS-rebinding TOCTOU between the resolve done here
and the resolve `requests` performs internally when it actually connects is
not closed with a custom IP-pinned transport. This endpoint sits behind
company authentication and per-user rate limiting (utils/rate_limit.py), so
the realistic threat -- a user directly pasting a private/link-local URL --
is fully blocked by the resolve-and-reject check below. Closing the
rebinding race would need a custom requests.HTTPAdapter that connects to a
pre-resolved, pinned IP; disproportionate effort for a first release. Future
hardening item, not a silent omission.
"""

import ipaddress
import logging
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {'http', 'https'}
ALLOWED_CONTENT_TYPES = ('text/html', 'text/plain')
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_TEXT_CHARS = 5000
FETCH_TIMEOUT_SECONDS = 10


class UnsafeURLError(Exception):
    """The URL (or a redirect target) resolves to a disallowed address. This
    is a hard failure for the caller -- never proceed as if the fetch had
    simply failed."""


class FetchFailedError(Exception):
    """An ordinary fetch problem (timeout, non-2xx, disallowed content
    type). The caller should degrade gracefully, not hard-fail."""


class _TextExtractor(HTMLParser):
    """Collects visible text, dropping <script>/<style> content."""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)

    def text(self) -> str:
        return ' '.join(self.chunks)


def _assert_host_is_safe(hostname: str):
    """Resolve `hostname` and reject if ANY resolved address is
    private/loopback/link-local/reserved/multicast/unspecified -- reject-any
    rather than reject-first, since a multi-answer DNS response could mix a
    public decoy with a private target."""
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f'Could not resolve host: {hostname}') from exc

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UnsafeURLError(f'URL resolves to a disallowed address: {ip_str}')


def _assert_url_is_safe(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f'Disallowed URL scheme: {parsed.scheme}')
    if not parsed.hostname:
        raise UnsafeURLError('URL has no hostname.')
    _assert_host_is_safe(parsed.hostname)


def fetch_text(url: str) -> str:
    """Fetch `url` and return extracted, truncated readable text.

    Raises UnsafeURLError (hard failure -- the caller must not proceed as if
    nothing happened) or FetchFailedError (ordinary failure -- the caller
    should degrade gracefully and continue without this content).
    """
    _assert_url_is_safe(url)

    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response = requests.get(
                current_url, timeout=FETCH_TIMEOUT_SECONDS, allow_redirects=False, stream=True,
            )
        except requests.RequestException as exc:
            raise FetchFailedError(f'Request failed: {exc}') from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise FetchFailedError('Redirect response had no Location header.')
            # A redirect target is attacker-controlled input too -- re-validate.
            _assert_url_is_safe(location)
            current_url = location
            continue

        if response.status_code != 200:
            response.close()
            raise FetchFailedError(f'Unexpected status code: {response.status_code}')

        content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            response.close()
            raise FetchFailedError(f'Disallowed content type: {content_type or "unknown"}')

        content_length = response.headers.get('Content-Length')
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            response.close()
            raise FetchFailedError('Response exceeds the maximum allowed size.')

        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                response.close()
                raise FetchFailedError('Response exceeds the maximum allowed size.')
        response.close()

        raw_text = body.decode('utf-8', errors='replace')
        if content_type == 'text/html':
            extractor = _TextExtractor()
            extractor.feed(raw_text)
            text = extractor.text()
        else:
            text = raw_text

        return text[:MAX_TEXT_CHARS]

    raise FetchFailedError('Too many redirects.')
