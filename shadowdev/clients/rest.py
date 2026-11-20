import json
import time
import urllib.error
import urllib.request

ATTEMPTS = 3
MAX_WAIT_SECONDS = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # a body over this is refused (413): no API answer is that big
MAX_ERROR_BYTES = 4096  # of an error answer's body, of which its first 500 characters are shown


class HTTPError(RuntimeError):
    def __init__(self, message: str, status: int, retry_after: str | None = None, location: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # the Retry-After header of a 429 or 503
        self.location = location  # where a redirect (3xx) points: it is not followed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Never follows a redirect: the Authorization header goes to the host asked, and nowhere else.
_direct = urllib.request.build_opener(_NoRedirect)


def request_json(method: str, url: str, headers: dict, body: dict | None = None, label: str = ""):
    """JSON request; raises HTTPError (with the status) on a non-2xx response."""
    return request(method, url, headers, body, label)[0]


def request(method: str, url: str, headers: dict, body: dict | None = None, label: str = ""):
    """Like request_json, but returns (JSON, response headers). A rate limit (429) is waited out
    and tried again, and so is a GET that hit a gateway error or could not connect; other
    requests are not repeated, since they may have been carried out. A server that cannot be
    reached at all raises HTTPError with status 0."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _request(method, url, headers, body, label)
        except HTTPError as e:
            again = e.status == 429 or (method == "GET" and e.status in (0, 502, 503, 504))
            if not again or attempt == ATTEMPTS:
                raise
            wait = float(e.retry_after) if (e.retry_after or "").isascii() and (e.retry_after or "").isdigit() else 2 ** attempt
            if wait > MAX_WAIT_SECONDS:
                raise
            time.sleep(wait)


def _request(method: str, url: str, headers: dict, body: dict | None, label: str):
    request = urllib.request.Request(  # noqa: S310
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
    )
    _, response_headers, data = _exchange(request, label, timeout=30, max_bytes=MAX_RESPONSE_BYTES)
    return (json.loads(data) if data else None), response_headers


def _exchange(request: urllib.request.Request, label: str, timeout: int, max_bytes: int):
    """Sends the request without following redirects. Returns (status, headers, body). Raises
    HTTPError: with the status for any answer that is not 2xx (a redirect too, with its `location`),
    413 for a body over `max_bytes`, and status 0 when no answer comes at all."""
    what = f"{label} {request.get_method()} {request.full_url.split('?')[0]}".strip()
    try:
        with _direct.open(request, timeout=timeout) as response:  # noqa: S310
            status, headers, body = response.status, response.headers, response.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        headers = e.headers or {}
        raise HTTPError(f"{what} -> {e.code}{_detail(e)}", e.code, headers.get("Retry-After"), headers.get("Location")) from e
    except OSError as e:  # URLError, a timeout, a dropped connection
        raise HTTPError(f"{what} could not be reached: {e}", 0) from e
    if len(body) > max_bytes:
        raise HTTPError(f"{what}: larger than {max_bytes} bytes", 413)
    return status, headers, body


def _detail(e: urllib.error.HTTPError) -> str:
    """The start of an error answer's body, for the message; none when it has no body."""
    try:
        return ": " + e.read(MAX_ERROR_BYTES).decode(errors="replace")[:500]
    except Exception:  # an error made without a body (e.g. in a test): nothing to show
        return ""


def download(url: str, headers: dict, max_bytes: int, label: str = "") -> bytes:
    """The body of a GET, for files. Redirects are refused, so the Authorization header never
    goes to another host. Raises HTTPError, with status 413 for a body over `max_bytes`."""
    _, _, data = _exchange(urllib.request.Request(url, headers=headers), label, timeout=60, max_bytes=max_bytes)  # noqa: S310
    return data


def redirect_location(url: str, headers: dict, label: str = "") -> str:
    """Where a GET redirects to, without following it (so the Authorization header stays here).
    Raises HTTPError when the answer is not a redirect to an https URL."""
    try:
        status, _, _ = _exchange(urllib.request.Request(url, headers=headers), label, timeout=30,  # noqa: S310
                                 max_bytes=MAX_RESPONSE_BYTES)
    except HTTPError as e:
        if 300 <= e.status < 400 and e.location and e.location.startswith("https://"):
            return e.location
        raise
    raise HTTPError(f"{label} GET {url.split('?')[0]}: {status}, not the redirect expected", status)
