"""Disposable SQLite service used by the deployment acceptance tests."""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

DATABASE = "/data/app.sqlite"


def initialize():
    with sqlite3.connect(DATABASE) as database:
        database.execute("CREATE TABLE IF NOT EXISTS metadata (version INTEGER NOT NULL)")
        database.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
        if not database.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]:
            database.execute("INSERT INTO metadata VALUES (1)")
            database.execute("INSERT INTO notes VALUES (1, 'kept')")


def state():
    with sqlite3.connect(DATABASE) as connection:
        return {
            "schema": connection.execute("SELECT version FROM metadata").fetchone()[0],
            "notes": [row[0] for row in connection.execute("SELECT body FROM notes ORDER BY id")],
        }


def migrate(target, fail):
    initialize()
    with sqlite3.connect(DATABASE) as connection:
        version = connection.execute("SELECT version FROM metadata").fetchone()[0]
        if target == 2 and version == 1:
            connection.execute("ALTER TABLE notes ADD COLUMN category TEXT DEFAULT 'general'")
            connection.execute("UPDATE notes SET body = 'converted'")
            connection.execute("UPDATE metadata SET version = 2")
    print("MIGRATION_COMMITTED", flush=True)
    if fail:
        raise RuntimeError("intentional failure after committed migration")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(state()).encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    initialize()
    HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
