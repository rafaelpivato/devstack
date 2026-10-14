#!/usr/bin/env python3
"""Temporary, single-purpose HTTP activation portal (no Devstack controls)."""
from __future__ import annotations

import argparse
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import parse_qs

URL_FILE = Path("/var/lib/devstack-bootstrap/activation-url")
SECRET_FILE = Path("/etc/devstack/bootstrap.env")
# Tailscale's CLI outputs an AuthURL on the first JSON event from 'up --json'.
LOGIN_URL = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+")
FAILURES: dict[str, tuple[int, float]] = {}


def extract_login_url(output: str) -> str:
    match = LOGIN_URL.search(output)
    if not match:
        raise ValueError("No Tailscale login URL found in CLI output")
    return match.group(0)


def valid_setup_secret(candidate: str, expected_sha256: str) -> bool:
    if not candidate or not expected_sha256:
        return False
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected_sha256)


def cleanup_after_activation() -> None:
    """Called only after the private Serve setup command succeeds."""
    if not SECRET_FILE.exists():
        return  # Nothing to close on legacy/interactive installations.
    subprocess.run(
        ["systemctl", "disable", "--now", "devstack-bootstrap.service"],
        check=True, capture_output=True, timeout=30,
    )
    SECRET_FILE.unlink(missing_ok=True)
    URL_FILE.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "DevstackSetup/1"
    sys_version = ""

    def send_protected_headers(self, status: int, mime: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_protected_headers(404)
            return
        self.send_protected_headers(200)
        self.wfile.write(b"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Devstack activation</title>
<style>body{font:16px system-ui,sans-serif;max-width:31rem;margin:3rem auto;
padding:0 1rem;line-height:1.55}input,button{display:block;box-sizing:border-box;
width:100%;padding:.85rem;margin:.7rem 0;font:inherit}button{cursor:pointer}
small{color:#555}</style>
<h1>Activate Devstack</h1>
<p>Enter the one-time setup secret you placed in EC2 User Data.</p>
<form action="/activate" method="post" autocomplete="off">
<input type="password" name="secret" placeholder="Setup secret"
autocomplete="off" required minlength="20" maxlength="256">
<button type="submit">Connect to Tailscale</button></form>
<small>Temporary HTTP setup only. The code is transmitted without encryption.
Prefer cellular connectivity. After approval, use the private HTTPS address
listed in Tailscale Machines. Never enter your Tailscale password here.</small>
</html>""")

    def do_POST(self) -> None:
        if self.path != "/activate":
            self.send_protected_headers(404)
            return
        ip = self.client_address[0]
        count, start = FAILURES.get(ip, (0, time.monotonic()))
        if time.monotonic() - start > 900:
            count, start = 0, time.monotonic()
        if count >= 6:
            self.send_protected_headers(429)
            self.wfile.write(b"Too many attempts. Try again later.")
            return
        length_str = self.headers.get("Content-Length", "")
        if not length_str.isdigit() or not 0 < int(length_str) <= 2048:
            self.send_protected_headers(400)
            return
        try:
            params = parse_qs(
                self.rfile.read(int(length_str)).decode("utf-8", errors="strict"),
                keep_blank_values=True,
            )
        except UnicodeError:
            self.send_protected_headers(400)
            return
        supplied = params.get("secret", [""])[0]
        expected = os.environ.get("DEVSTACK_BOOTSTRAP_HASH", "")
        if not valid_setup_secret(supplied, expected):
            FAILURES[ip] = (count + 1, start)
            self.send_protected_headers(403)
            self.wfile.write(b"Invalid activation secret.")
            return
        try:
            url = extract_login_url(URL_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.send_protected_headers(503)
            self.wfile.write(b"Authentication URL not yet available. Check Tailscale setup.")
            return
        FAILURES.pop(ip, None)
        # Don't rely on cross-site POST redirects on mobile browsers.
        # Render a clear, tappable link so successful validation is visible.
        safe_url = escape(url, quote=True)
        html = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Authorize Devstack</title>'
            '<style>body{font:16px system-ui,sans-serif;max-width:31rem;'
            'margin:3rem auto;padding:0 1rem;line-height:1.6}'
            'a{display:block;padding:1rem;border:1px solid;'
            'border-radius:.5rem;text-align:center}</style>'
            '<h1>Setup code accepted</h1>'
            '<p>Open the official Tailscale website to authorize this server.</p>'
            f'<a href="{safe_url}" rel="noreferrer noopener">'
            'Continue to Tailscale</a>'
            '<p>After approving the device, find your private HTTPS address '
            'in the Tailscale Machines page. Do not enter your setup code '
            'or Tailscale credentials into any other website.</p></html>'
        )
        self.send_protected_headers(200)
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        # Never log form values, query strings, or the authorization URL.
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finish", action="store_true")
    args = parser.parse_args()
    if args.finish:
        cleanup_after_activation()
    else:
        expected = os.environ.get("DEVSTACK_BOOTSTRAP_HASH", "")
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise SystemExit("Devstack bootstrap secret hash is not configured")
        ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()


if __name__ == "__main__":
    main()
