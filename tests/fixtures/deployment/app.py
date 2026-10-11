"""Disposable app with real health, build records, and SQLite migrations."""

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from database import migrate

VERSION = "one"
SCHEMA_VERSION = 1
FAIL_MIGRATION = False
FAIL_HEALTH = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version.json":
            body = {"version": VERSION, "revision": os.environ.get("APP_REVISION", "")}
            status = 200
        else:
            try:
                with urllib.request.urlopen("http://db:9000/state", timeout=2) as response:
                    body = json.load(response)
                body["version"] = VERSION
                status = 200 if body["schema"] == SCHEMA_VERSION and not FAIL_HEALTH else 503
            except OSError:
                body, status = {"error": "database unavailable"}, 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["migrate"]:
        migrate(SCHEMA_VERSION, FAIL_MIGRATION)
    else:
        HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
