"""Download probe regressions against a local binary/HTML/error/slow server."""
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server_speed_probe as probe


BODY = bytes(range(256)) * 1024


class Fixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.server.lock:
            self.server.active += 1
            self.server.maximum_active = max(self.server.maximum_active, self.server.active)
            self.server.requests += 1
            number = self.server.requests
        try:
            content = BODY
            code, mime = 200, "application/octet-stream"
            if self.path == "/html":
                content, mime = b"<html>error</html>" * 8192, "text/html"
            elif self.path == "/json":
                content, mime = b'{"error":"not a download"}' * 8192, "application/problem+json"
            elif self.path == "/error":
                code = 503
            elif self.path == "/tiny":
                content = b"small"
            elif self.path == "/partial":
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.path == "/stagger":
                time.sleep(.1 if number % 2 else .5)
            for offset in range(0, len(content), 8192):
                self.wfile.write(content[offset:offset + 8192])
                self.wfile.flush()
                if self.path == "/slow":
                    time.sleep(.06)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.lock:
                self.server.active -= 1


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@contextmanager
def fixture_server():
    server = Server(("127.0.0.1", 0), Fixture)
    server.lock = threading.Lock()
    server.active = server.maximum_active = server.requests = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@unittest.skipUnless(shutil.which("curl"), "curl is required")
class DownloadProbe(unittest.TestCase):
    def fetch(self, path, **kwargs):
        with fixture_server() as (server, base):
            result = probe.probe_download(base + path, allow_http=True, seconds=1, **kwargs)
            return result

    def test_real_binary_download_ignores_inherited_proxy_and_does_not_echo_url(self):
        with fixture_server() as (_, base), mock.patch.dict(os.environ, {
                "http_proxy": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
            result = probe.probe_download(base + "/binary?secret=do-not-print", allow_http=True, seconds=1)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["bytes"], len(BODY))
        self.assertGreater(result["mbps"], 0)
        self.assertNotIn("do-not-print", json.dumps(result))
        self.assertNotIn("127.0.0.1", json.dumps(result))

    def test_html_json_http_error_and_tiny_body_never_count_as_speed(self):
        for path, reason in (("/html", "non_binary_response"), ("/json", "non_binary_response"),
                             ("/error", "http_response_rejected"), ("/tiny", "sample_too_small")):
            with self.subTest(path=path):
                result = self.fetch(path)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["bytes"], 0)
                self.assertIsNone(result["mbps"])
                self.assertEqual(result["samples"][0]["error"], reason)

    def test_timed_binary_sample_is_valid_when_it_transferred_enough_data(self):
        result = self.fetch("/slow")
        self.assertEqual(result["status"], "ok", result)
        sample = result["samples"][0]
        self.assertEqual(sample["status"], "timed_sample", result)
        self.assertEqual(sample["curl_exit"], 28)
        self.assertGreaterEqual(result["bytes"], probe.MIN_SAMPLE_BYTES)
        self.assertLess(result["bytes"], len(BODY))

    def test_partial_content_response_is_a_valid_binary_sample(self):
        result = self.fetch("/partial")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["samples"][0]["http_code"], 206)
        self.assertEqual(result["bytes"], len(BODY))

    def test_parallel_probe_overlaps_and_uses_actual_group_wall_time(self):
        with fixture_server() as (server, base):
            result = probe.probe_download(base + "/stagger", connections=4,
                                          allow_http=True, seconds=1)
            maximum_active = server.maximum_active
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["bytes"], len(BODY) * 4)
        self.assertGreaterEqual(maximum_active, 2)
        expected = result["bytes"] * 8 / result["elapsed_seconds"] / 1_000_000
        self.assertAlmostEqual(result["mbps"], expected, delta=.01)
        sum_average = sum(s["bytes"] * 8 / s["elapsed_seconds"] / 1_000_000
                          for s in result["samples"])
        self.assertLess(result["mbps"], sum_average * .8)

    def test_https_only_and_bounded_parameters_fail_before_curl(self):
        with mock.patch.object(probe.subprocess, "run") as run:
            for url, kwargs in (("http://example.test/file", {}),
                                ("https://user:password@example.test/file", {}),
                                ("https://example.test/file", {"seconds": 16}),
                                ("https://example.test/file", {"connections": 5}),
                                ("https://example.test/file", {"proxy": "socks5h://example.test:1080"})):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    probe.probe_download(url, **kwargs)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
