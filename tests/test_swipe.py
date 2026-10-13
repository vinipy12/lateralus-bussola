import http.client
import json
import tempfile
import threading
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock
from contextlib import closing
from pathlib import Path

import store
from helpers import REPO, write_profile
from radar import Job
from swipe import make_server

KEY = "test-key-0123456789abcdef"
CARD = {"summary": "s", "eligible": "unclear", "eligible_quote": None, "timezone": None, "compensation": None, "seniority": None,
        "must_haves": [], "pros": [], "cons": [], "dealbreaker": None, "dealbreaker_quote": None}


class SwipeServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        write_profile(root / "profiles", "example")
        (root / "profiles" / "broken").mkdir()
        (root / "profiles" / "broken" / "profile.json").write_text("{}")
        with closing(store.connect(root / "out" / "example" / "radar.db")) as db, db:
            store.upsert_jobs(db, [(7, Job("Acme", "AI Engineer", "Remote", "https://example.com/1", "d", "Ashby"), ["r"])], "t")
            store.save_card(db, "https://example.com/1", CARD, 0, "t")
        self.server = make_server(KEY, root / "profiles", root / "out", REPO / "swipe.html", host="127.0.0.1", port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = f"bussola_key={KEY}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response, (json.loads(data) if response.getheader("Content-Type") == "application/json" and data else data)

    def test_without_key_everything_is_404(self):
        for method, path in [("GET", "/"), ("GET", "/api/profiles"), ("GET", "/api/deck?profile=example"), ("GET", "/?k=wrong")]:
            self.assertEqual(self.request(method, path)[0], 404, path)
        self.assertEqual(self.request("POST", "/api/undo", {"profile": "example"}, cookie="bussola_key=nope")[0], 404)

    def test_query_key_sets_http_only_cookie_and_redirects(self):
        status, response, _ = self.request("GET", f"/?k={KEY}")
        self.assertEqual(status, 302)
        self.assertEqual(response.getheader("Location"), "/")
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertEqual(self.request("GET", "/", cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("GET", "/api/profiles", cookie=self.cookie)[2], {"profiles": ["broken", "example"]})

    def test_swipe_double_swipe_undo_and_empty_undo(self):
        deck = lambda: self.request("GET", "/api/deck?profile=example", cookie=self.cookie)[2]["cards"]
        self.assertEqual(len(deck()), 1)
        body = {"profile": "example", "url": "https://example.com/1", "decision": "apply"}
        self.assertEqual(self.request("POST", "/api/swipe", body, cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("POST", "/api/swipe", body, cookie=self.cookie)[0], 200)
        self.assertEqual(deck(), [])
        applies = self.request("GET", "/api/applies?profile=example", cookie=self.cookie)[2]["applies"]
        self.assertEqual([a["url"] for a in applies], ["https://example.com/1"])
        self.assertEqual(self.request("POST", "/api/undo", {"profile": "example"}, cookie=self.cookie)[2], {"undone": "https://example.com/1"})
        self.assertEqual(len(deck()), 1)
        self.assertEqual(self.request("POST", "/api/undo", {"profile": "example"}, cookie=self.cookie)[2], {"undone": None})

    def test_bad_input(self):
        swipe = lambda body: self.request("POST", "/api/swipe", body, cookie=self.cookie)[0]
        self.assertEqual(swipe({"profile": "example", "url": "https://example.com/1", "decision": "maybe"}), 400)
        self.assertEqual(swipe({"profile": "example", "url": "https://example.com/unknown", "decision": "pass"}), 404)
        self.assertEqual(swipe({"profile": "../etc", "url": "https://example.com/1", "decision": "pass"}), 404)
        self.assertEqual(self.request("GET", "/api/deck?profile=nobody", cookie=self.cookie)[0], 404)
        self.assertEqual(self.request("GET", "/api/deck?profile=broken", cookie=self.cookie)[0], 409)

    def test_every_method_without_key_is_404(self):
        for method in ["HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"]:
            self.assertEqual(self.request(method, "/")[0], 404, method)
            self.assertEqual(self.request(method, "/nope", cookie=self.cookie)[0], 404, method)

    def test_unexpected_error_is_500_json_and_server_keeps_serving(self):
        err = io.StringIO()
        with mock.patch.object(store, "deck", side_effect=RuntimeError("boom")), redirect_stderr(err):
            status, _, body = self.request("GET", "/api/deck?profile=example&k=" + KEY, cookie=self.cookie)
        self.assertEqual((status, body), (500, {"error": "internal error"}))
        self.assertIn("GET /api/deck: RuntimeError", err.getvalue())
        self.assertNotIn(KEY, err.getvalue())
        self.assertEqual(self.request("GET", "/api/profiles", cookie=self.cookie)[0], 200)

    def test_deeply_nested_json_is_400(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/swipe", body="[" * 5000, headers={"Content-Type": "application/json", "Cookie": self.cookie})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 400)

    def test_weak_key_is_refused(self):
        with self.assertRaises(ValueError):
            make_server("short", Path("."), Path("."), REPO / "swipe.html", host="127.0.0.1", port=0)


if __name__ == "__main__":
    unittest.main()
