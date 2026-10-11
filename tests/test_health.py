"""`build_probe`: the served build record against the checkout's commit."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from srvr.core.health import build_probe


class BuildProbeTest(unittest.TestCase):
    def serve(self, status: int, body: bytes) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/version.json"

    def test_matching_revision_passes(self) -> None:
        url = self.serve(200, json.dumps({"revision": "a1b2c3d4e5"}).encode())
        result = build_probe(url, "a1b2c3d4e5")
        self.assertEqual((result.ok, result.detail), (True, "serves a1b2c3d"))

    def test_other_revision_fails_and_names_both(self) -> None:
        url = self.serve(200, json.dumps({"revision": "dev"}).encode())
        result = build_probe(url, "a1b2c3d4e5")
        self.assertEqual((result.ok, result.detail), (False, "serves dev, checkout is a1b2c3d"))

    def test_missing_or_garbled_record_fails(self) -> None:
        self.assertEqual(build_probe(self.serve(404, b""), "a1b2c3d").detail, "HTTP 404")
        self.assertEqual(
            build_probe(self.serve(200, b"<html>"), "a1b2c3d").detail, "not a build record"
        )
        self.assertEqual(
            build_probe(self.serve(200, b"[]"), "a1b2c3d").detail, "not a build record"
        )
