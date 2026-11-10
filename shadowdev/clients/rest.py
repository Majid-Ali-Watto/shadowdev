import json
import time
import urllib.error
import urllib.request

ATTEMPTS = 3
MAX_WAIT_SECONDS = 20


class HTTPError(RuntimeError):
    def __init__(self, message: str, status: int, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # the Retry-After header of a 429 or 503


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
            wait = float(e.retry_after) if (e.retry_after or "").isdigit() else 2 ** attempt
            if wait > MAX_WAIT_SECONDS:
                raise
            time.sleep(wait)


def _request(method: str, url: str, headers: dict, body: dict | None, label: str):
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data, response_headers = response.read(), response.headers
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise HTTPError(f"{label} {method} {url.split('?')[0]} -> {e.code}: {detail}", e.code,
                        e.headers.get("Retry-After") if e.headers else None) from e
    except OSError as e:  # URLError, a timeout, a dropped connection
        raise HTTPError(f"{label} {method} {url.split('?')[0]} could not be reached: {e}", 0) from e
    return (json.loads(data) if data else None), response_headers
