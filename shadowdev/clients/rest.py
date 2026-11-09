import json
import urllib.error
import urllib.request


class HTTPError(RuntimeError):
    def __init__(self, message: str, status: int, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # the Retry-After header of a 429 or 503


def request_json(method: str, url: str, headers: dict, body: dict | None = None, label: str = ""):
    """JSON request; raises HTTPError (with the status) on a non-2xx response."""
    return request(method, url, headers, body, label)[0]


def request(method: str, url: str, headers: dict, body: dict | None = None, label: str = ""):
    """Like request_json, but returns (JSON, response headers)."""
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
    return (json.loads(data) if data else None), response_headers
