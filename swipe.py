#!/usr/bin/env python3
"""Serve the swipe deck and its JSON API on the home network. Every request without SWIPE_KEY gets a 404."""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import store
from profiles import NAME, ProfileError, list_profiles, load_profile

HERE = Path(__file__).resolve().parent
COOKIE = "bussola_key"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,}")
MAX_BODY = 16_384
APPLY_WINDOW = timedelta(days=7)
CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def make_server(key: str, profiles_dir: Path, out_dir: Path, page: Path,
                host: str = "0.0.0.0", port: int = 8765) -> ThreadingHTTPServer:
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError("SWIPE_KEY must be at least 16 characters of letters, digits, '-' or '_'")
    secret = key.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "bussola"
        sys_version = ""
        timeout = 10

        def log_message(self, format, *args) -> None:
            # The default access log would print "GET /?k=<SWIPE_KEY>" and spam test output.
            pass

        def do_GET(self) -> None:
            self.dispatch("GET")

        def do_POST(self) -> None:
            self.dispatch("POST")

        def do_HEAD(self) -> None:
            self.dispatch("HEAD")

        def do_OPTIONS(self) -> None:
            self.dispatch("OPTIONS")

        def do_PUT(self) -> None:
            self.dispatch("PUT")

        def do_DELETE(self) -> None:
            self.dispatch("DELETE")

        def do_PATCH(self) -> None:
            self.dispatch("PATCH")

        def dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            query = {name: values[0] for name, values in parse_qs(parts.query).items()}
            from_query = self.valid(query.get("k", ""))
            if not (from_query or self.valid(self.cookie_key())):
                return self.send(404, {"error": "not found"})
            if from_query and method == "GET" and parts.path == "/":
                # Drop the key from the address bar once the cookie holds it.
                return self.send(302, None, {"Location": "/"}, set_cookie=True)
            routes = {("GET", "/"): self.page, ("GET", "/api/profiles"): self.profiles, ("GET", "/api/deck"): self.deck,
                      ("GET", "/api/applies"): self.applies, ("POST", "/api/swipe"): self.swipe, ("POST", "/api/undo"): self.undo}
            route = routes.get((method, parts.path))
            if route is None:
                return self.send(404, {"error": "not found"}, set_cookie=from_query)
            try:
                status, body = route(query)
            except ProfileError as exc:
                status, body = 409, {"error": f"profile is invalid: {exc}"}
            except Exception as exc:
                # Path only: the query string can carry the key.
                print(f"{method} {parts.path}: {type(exc).__name__}", file=sys.stderr)
                status, body = 500, {"error": "internal error"}
            self.send(status, body, set_cookie=from_query)

        def valid(self, candidate: str) -> bool:
            return bool(candidate) and hmac.compare_digest(candidate.encode(), secret)

        def cookie_key(self) -> str:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except CookieError:
                return ""
            return jar[COOKIE].value if COOKIE in jar else ""

        def send(self, status: int, body, headers: dict | None = None, set_cookie: bool = False) -> None:
            if isinstance(body, bytes):
                data, content_type = body, "text/html; charset=utf-8"
            else:
                data, content_type = (b"" if body is None else json.dumps(body).encode()), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if set_cookie:
                self.send_header("Set-Cookie", f"{COOKIE}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
            self.end_headers()
            self.wfile.write(data)

        def json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if not 0 < length <= MAX_BODY:
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return None
            return body if isinstance(body, dict) else None

        def open_profile(self, name: object):
            # Only names listed on disk reach the filesystem, so "../x" never does.
            if not isinstance(name, str) or not NAME.fullmatch(name) or name not in list_profiles(profiles_dir):
                return None
            return load_profile(profiles_dir / name)

        def database(self, profile):
            return closing(store.connect(out_dir / profile.name / "radar.db"))

        def page(self, query):
            return 200, page.read_bytes()

        def profiles(self, query):
            return 200, {"profiles": list_profiles(profiles_dir)}

        def deck(self, query):
            profile = self.open_profile(query.get("profile"))
            if profile is None:
                return 404, {"error": "unknown profile"}
            with self.database(profile) as db:
                return 200, {"profile": profile.name, "country": profile.country, "cards": store.deck(db, profile)}

        def applies(self, query):
            profile = self.open_profile(query.get("profile"))
            if profile is None:
                return 404, {"error": "unknown profile"}
            since = (datetime.now(timezone.utc) - APPLY_WINDOW).isoformat(timespec="microseconds")
            with self.database(profile) as db:
                return 200, {"applies": store.applies(db, since)}

        def swipe(self, query):
            body = self.json_body()
            if body is None or body.get("decision") not in store.DECISIONS or not isinstance(body.get("url"), str):
                return 400, {"error": "expected {profile, url, decision: pass|apply}"}
            profile = self.open_profile(body.get("profile"))
            if profile is None:
                return 404, {"error": "unknown profile"}
            with self.database(profile) as db, db:
                recorded = store.record_swipe(db, body["url"], body["decision"], stamp())
            return (200, {"ok": True}) if recorded else (404, {"error": "unknown job"})

        def undo(self, query):
            body = self.json_body()
            profile = self.open_profile(body.get("profile") if body else None)
            if profile is None:
                return 404, {"error": "unknown profile"}
            with self.database(profile) as db, db:
                return 200, {"undone": store.undo_last(db)}

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    try:
        server = make_server(os.environ.get("SWIPE_KEY", ""), HERE / "profiles", HERE / "out", HERE / "swipe.html",
                             port=int(os.environ.get("SWIPE_PORT", "8765")))
    except ValueError as exc:
        print(f"{exc}. Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(24))'", file=sys.stderr)
        return 1
    print(f"Swipe deck listening on port {server.server_address[1]}; open http://<laptop>:{server.server_address[1]}/?k=<SWIPE_KEY>", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
