"""A stand-in for Home Assistant around the page, for the Page check
(scripts/screenshots.py). It runs in the built image at the ingress proxy's
address 172.30.32.2, the only peer the page lets in, and serves on port 8080:

    /                               a page that holds the app in a frame, as Home Assistant does
    /api/hassio_ingress/<token>/... the app's page, passed on to APP with the
                                    path prefix taken off and X-Ingress-Path set, like the Supervisor
    /config/app/<slug>/config       where the settings button leads (outside the frame)

    python3 /tests/page/ingress.py http://<app>:8099
"""

from __future__ import annotations

import html
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/hassio_ingress/demo-token"
APP = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8099"
PASS_ON = ("Accept-Language", "Content-Type", "Content-Length")
OUTER = f"""<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title>
<style>html, body, iframe {{ margin: 0; border: 0; width: 100%; height: 100%; display: block; }}</style></head>
<body><home-assistant><iframe id="ingress" title="app" src="{PREFIX}/"></iframe></home-assistant></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        path = self.path[len(PREFIX):] or "/"
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k in PASS_ON if (v := self.headers.get(k))}
        headers["X-Ingress-Path"] = PREFIX
        request = urllib.request.Request(APP + path, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                self._send(answer.status, answer.headers, answer.read())
        except urllib.error.HTTPError as err:
            self._send(err.code, err.headers, err.read())

    def _send(self, code: int, headers, body: bytes) -> None:
        self.send_response(code)
        for key, value in headers.items():
            if key.lower() not in ("connection", "transfer-encoding", "content-length", "server", "date"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, text: str) -> None:
        self._send(200, {"Content-Type": "text/html; charset=utf-8"}, text.encode())

    def do_GET(self) -> None:  # noqa: N802 (the standard library's name)
        if self.path.startswith(PREFIX + "/"):
            self._forward()
        elif self.path == "/":
            self._page(OUTER)
        elif self.path == "/favicon.ico":
            # Home Assistant has one; the browser asks for it also for a page in the frame
            self._send(200, {"Content-Type": "image/x-icon"}, b"")
        elif self.path.startswith("/config/app/"):
            self._page(f"<!doctype html><title>Settings</title><h1>{html.escape(self.path)}</h1>")
        else:
            self._send(404, {"Content-Type": "text/plain"}, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith(PREFIX + "/"):
            self._forward()
        else:
            self._send(404, {"Content-Type": "text/plain"}, b"not found")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
