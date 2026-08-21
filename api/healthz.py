"""Lightweight Vercel health endpoint."""
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

VERSION = "2026.08.21-universal-gaming-radar-v2-platform-vercel"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({
            "ok": True,
            "app": "Universal Gaming Radar",
            "version": VERSION,
            "mode": "vercel-request-driven",
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)
