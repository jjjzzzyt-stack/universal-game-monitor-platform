#!/usr/bin/env python3
"""Small local harness that emulates the Vercel routes for development."""
import json
import mimetypes
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import state as state_api

ROOT = pathlib.Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_bytes(self, data, content_type, status=200, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/healthz":
            self.send_bytes(json.dumps({"ok": True, "app": "Universal Gaming Radar", "version": state_api.CLOUD_VERSION, "mode": "local-vercel-harness"}).encode(), "application/json")
        elif path == "/api/state":
            self.send_bytes(json.dumps(state_api.build_state({}), ensure_ascii=False).encode(), "application/json")
        elif path in {"/", "/index.html"}:
            self.send_bytes((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            file = ROOT / path.lstrip("/")
            try:
                file = file.resolve()
                if not file.is_file() or ROOT.resolve() not in file.parents:
                    raise FileNotFoundError
                self.send_bytes(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream", cache="public, max-age=300")
            except (OSError, FileNotFoundError):
                self.send_bytes(b"Not found", "text/plain", 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/state":
            self.send_bytes(b'{"error":"Not found"}', "application/json", 404)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), state_api.MAX_BODY_BYTES)
            body = json.loads(self.rfile.read(length)) if length else {}
            result = state_api.build_state(body)
            self.send_bytes(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(), "application/json")
        except Exception as exc:
            self.send_bytes(json.dumps({"error": str(exc)}).encode(), "application/json", 500)


if __name__ == "__main__":
    print("Vercel-compatible Gaming Radar preview: http://127.0.0.1:8897/")
    ThreadingHTTPServer(("127.0.0.1", 8897), Handler).serve_forever()
