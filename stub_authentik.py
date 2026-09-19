import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer stubtoken123":
            body = json.dumps({"detail": "Authentication credentials were not provided."}).encode()
            self.send_response(401)
        elif self.path.startswith("/api/v3/core/users/me/"):
            body = json.dumps({"user": {"username": "stub-service-account"}}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v3/core/users/"):
            body = json.dumps({"pagination": {"count": 0}, "results": []}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v3/admin/system/"):
            body = json.dumps({"runtime": {"authentik_version": "2026.1.0"}}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
