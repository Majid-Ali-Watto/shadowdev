"""The HTTP client (clients/rest.py) against a local server: a redirect is not followed, so the
Authorization header never reaches another host, and a body over the cap is refused."""
import http.server
import threading
import unittest
from unittest import mock

from shadowdev.clients import rest


def serve(routes: dict, seen: list, test: unittest.TestCase) -> str:
    """A local server answering GET with routes[path] = (status, headers, body), and recording each
    (path, Authorization header) it gets in `seen`. Returns its base URL."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            status, headers, body = routes.get(self.path, (404, {}, b"{}"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_address[1]}"


class RedirectTest(unittest.TestCase):
    def setUp(self):
        self.seen_elsewhere = []  # what the other host got
        self.elsewhere = serve({"/file": (200, {}, b"secret")}, self.seen_elsewhere, self)
        self.seen = []
        self.routes = {}
        self.base = serve(self.routes, self.seen, self)
        self.routes["/start"] = (302, {"Location": self.elsewhere + "/file"}, b"")
        self.routes["/same"] = (302, {"Location": "/file"}, b"")
        self.routes["/file"] = (200, {}, b'{"ok": 1}')

    def test_json_redirect_is_an_error_and_the_other_host_gets_nothing(self):
        with self.assertRaises(rest.HTTPError) as caught:
            rest.request_json("GET", self.base + "/start", {"Authorization": "Bearer tok"})
        self.assertEqual((caught.exception.status, caught.exception.location), (302, self.elsewhere + "/file"))
        self.assertEqual(self.seen_elsewhere, [])  # the token was not sent there

    def test_a_redirect_on_the_same_host_is_not_followed_either(self):
        with self.assertRaises(rest.HTTPError) as caught:
            rest.request_json("GET", self.base + "/same", {})
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual([path for path, _ in self.seen], ["/same"])

    def test_a_download_is_refused_the_same_way(self):
        with self.assertRaises(rest.HTTPError) as caught:
            rest.download(self.base + "/start", {"Authorization": "Bearer tok"}, 1000, "Test")
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(self.seen_elsewhere, [])

    def test_redirect_location_reads_the_target_without_fetching_it(self):
        self.routes["/https"] = (302, {"Location": "https://logs.example/x"}, b"")
        self.routes["/plain"] = (302, {"Location": "http://plain.example/x"}, b"")
        self.assertEqual(rest.redirect_location(self.base + "/https", {"Authorization": "Bearer tok"}),
                         "https://logs.example/x")
        with self.assertRaises(rest.HTTPError):  # only an https URL is accepted
            rest.redirect_location(self.base + "/plain", {})
        self.assertEqual(self.seen_elsewhere, [])

    def test_a_plain_answer_is_read(self):
        self.assertEqual(rest.request_json("GET", self.base + "/file", {"Authorization": "Bearer tok"}), {"ok": 1})
        self.assertEqual(self.seen, [("/file", "Bearer tok")])


class BodyCapTest(unittest.TestCase):
    def setUp(self):
        self.routes = {"/big": (200, {}, b'{"data": "' + b"x" * 200 + b'"}'), "/small": (200, {}, b'{"n": 1}')}
        self.base = serve(self.routes, [], self)

    def test_a_body_over_the_cap_is_refused_with_413(self):
        with mock.patch.object(rest, "MAX_RESPONSE_BYTES", 100), self.assertRaises(rest.HTTPError) as caught:
            rest.request_json("GET", self.base + "/big", {})
        self.assertEqual(caught.exception.status, 413)
        with mock.patch.object(rest, "MAX_RESPONSE_BYTES", 100):
            self.assertEqual(rest.request_json("GET", self.base + "/small", {}), {"n": 1})

    def test_a_download_over_its_size_is_refused_with_413(self):
        with self.assertRaises(rest.HTTPError) as caught:
            rest.download(self.base + "/big", {}, 50, "Test")
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(rest.download(self.base + "/small", {}, 50, "Test"), b'{"n": 1}')

    def test_an_error_answer_says_its_status_and_start(self):
        with self.assertRaises(rest.HTTPError) as caught:
            rest.request_json("GET", self.base + "/missing", {}, label="Jira")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("Jira GET", str(caught.exception))
        self.assertIn("-> 404", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
