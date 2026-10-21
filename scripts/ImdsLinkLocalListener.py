#!/usr/bin/env python3
# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""
HTTP proxy on 169.254.169.254 for EC2 IMDSv2. Forwards to the MiniStack gateway
and sets X-Ministack-Imds-Client-Ip from the TCP peer address.
"""

from __future__ import annotations

import os
import sys
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

GATEWAY = os.environ.get("MINISTACK_IMDS_GATEWAY_URL", "http://127.0.0.1:4566").rstrip("/")
BIND_HOST = os.environ.get("MINISTACK_IMDS_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("MINISTACK_IMDS_PORT", "80"))
CLIENT_IP_HEADER = "X-Ministack-Imds-Client-Ip"
LINK_LOCAL_HEADER = "X-Ministack-Imds-Link-Local"

_parsed = urlparse(GATEWAY)
_GATEWAY_HOST = _parsed.hostname or "127.0.0.1"
_GATEWAY_PORT = _parsed.port or (443 if _parsed.scheme == "https" else 80)
_GATEWAY_SCHEME = _parsed.scheme or "http"


class _ImdsProxyHandler(BaseHTTPRequestHandler):
    """Forward IMDS requests to the MiniStack gateway."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _forward(self) -> None:
        client_ip = self.client_address[0]
        path = self.path or "/"
        body = b""
        if self.command in ("PUT", "POST", "PATCH"):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 0:
                body = self.rfile.read(length)

        forward_headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in ("host", "connection", "content-length"):
                continue
            forward_headers[key] = value
        forward_headers[CLIENT_IP_HEADER] = client_ip
        forward_headers[LINK_LOCAL_HEADER] = "1"

        try:
            conn = HTTPConnection(_GATEWAY_HOST, _GATEWAY_PORT, timeout=30)
            conn.request(self.command, path, body=body, headers=forward_headers)
            response = conn.getresponse()
            resp_body = response.read()
            conn.close()
        except (HTTPException, OSError) as err:
            payload = f"gateway unreachable: {err}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(response.status)
        for key, value in response.getheaders():
            lower = key.lower()
            if lower in ("transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()


def main() -> int:
    """
    Run the link-local IMDS proxy.

    @returns Process exit code (0 on success).
    """
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), _ImdsProxyHandler)
    print(f"IMDS link-local proxy on {BIND_HOST}:{BIND_PORT} -> {GATEWAY}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
