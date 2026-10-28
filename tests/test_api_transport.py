"""Verify local panel credentials bypass configured HTTP proxy environments."""
from __future__ import annotations

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parents[1]
BASH = (str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        if os.name == "nt" else shutil.which("bash"))


class LocalApiTransport(unittest.TestCase):
    def test_environment_proxy_does_not_receive_local_admin_requests(self):
        if not BASH or not Path(BASH).is_file():
            self.skipTest("Bash is required")
        direct_requests = []
        proxy_requests = []

        class PanelHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                direct_requests.append((self.command, self.path,
                                        self.headers.get("Authorization")))
                body = json.dumps({"success": True, "obj": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.do_GET()

            def log_message(self, *_):
                pass

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy_requests.append((self.command, self.path))
                self.send_response(502)
                self.end_headers()

            do_POST = do_GET
            do_CONNECT = do_GET

            def log_message(self, *_):
                pass

        with ExitStack() as stack:
            panel = ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler)
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            for server in (panel, proxy):
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                stack.callback(server.server_close)
                stack.callback(worker.join, 3)
                stack.callback(server.shutdown)
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="api-transport-")))
            binary = root / "x-ui"
            binary.write_text("#!/usr/bin/env bash\nprintf 'apiToken: isolated-fixture-token\\n'\n",
                              encoding="utf-8", newline="\n")
            binary.chmod(0o700)
            proxy_url = "http://127.0.0.1:" + str(proxy.server_port)
            env = dict(os.environ, API_TEST_REPO=REPO.as_posix(), API_TEST_ROOT=root.as_posix(),
                       API_TEST_PORT=str(panel.server_port), http_proxy=proxy_url,
                       https_proxy=proxy_url, all_proxy=proxy_url,
                       HTTP_PROXY=proxy_url, HTTPS_PROXY=proxy_url, ALL_PROXY=proxy_url,
                       no_proxy="", NO_PROXY="")
            source = r'''
source "$API_TEST_REPO/install.sh"
RUN_DIR="$API_TEST_ROOT"
XUI_INSTALL_DIR="$API_TEST_ROOT"
PANEL_API_SCHEME=http
PANEL_API_HOST=127.0.0.1
PANEL_PORT="$API_TEST_PORT"
WEB_BASE_PATH=audit
api_init
api_request POST '/panel/api/server/restartXrayService' '{}' >/dev/null
printf 'API_TRANSPORT_OK\n'
'''
            result = subprocess.run([BASH, "-c", source], env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=20)
            self.assertEqual(proxy_requests, [], "Local API request reached the configured proxy")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("API_TRANSPORT_OK", result.stdout)
            self.assertEqual([(method, path) for method, path, _ in direct_requests], [
                ("GET", "/audit/panel/api/inbounds/list"),
                ("POST", "/audit/panel/api/server/restartXrayService"),
            ])
            self.assertTrue(all(auth == "Bearer isolated-fixture-token"
                                for _, _, auth in direct_requests))


if __name__ == "__main__":
    unittest.main()
