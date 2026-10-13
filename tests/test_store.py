import tempfile
import unittest
from pathlib import Path

import store
from helpers import make_profile
from radar import Job

CARD = {"summary": "Build agents.", "eligible": "yes", "eligible_quote": "Brazil", "timezone": None, "compensation": None,
        "seniority": "Senior", "must_haves": ["Python"], "pros": [], "cons": [], "dealbreaker": None, "dealbreaker_quote": None}


def job(n, score=6, company="Acme", title=None, salary="", usd=None):
    return (score, Job(company, title or f"AI Engineer {n}", "Remote", f"https://example.com/{n}", "Python LLM agents",
                       "Ashby", "", salary, usd, True), ["core term 'ai' in title"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = make_profile(self.root / "profiles")
        self.db = store.connect(self.root / "out" / "example" / "radar.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_connect_uses_wal_and_busy_timeout(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_upsert_updates_fields_but_keeps_first_queued_at(self):
        with self.db:
            store.upsert_jobs(self.db, [job(1)], "2026-09-28T00:00:00+00:00")
            store.upsert_jobs(self.db, [job(1, score=9)], "2026-10-05T00:00:00+00:00")
        row = self.db.execute("SELECT score, queued_at, reasons_json FROM jobs").fetchone()
        self.assertEqual((row["score"], row["queued_at"]), (9, "2026-09-28T00:00:00+00:00"))
        self.assertIn("core term", row["reasons_json"])

    def test_pending_skips_carded_exhausted_and_swiped_jobs(self):
        with self.db:
            store.upsert_jobs(self.db, [job(n) for n in range(1, 5)], "t")
            store.save_card(self.db, "https://example.com/1", CARD, 0, "t")
            for _ in range(3):
                store.record_failure(self.db, "https://example.com/2", "timed out", "t")
            store.record_swipe(self.db, "https://example.com/3", "pass", "t")
        self.assertEqual([j["url"] for j in store.pending_jobs(self.db)], ["https://example.com/4"])

    def test_third_failure_turns_into_a_basic_card(self):
        with self.db:
            store.upsert_jobs(self.db, [job(1)], "t")
            store.record_failure(self.db, "https://example.com/1", "timed out", "t")
            store.record_failure(self.db, "https://example.com/1", "timed out", "t")
        self.assertEqual(len(store.pending_jobs(self.db)), 1)
        self.assertEqual(store.deck(self.db, self.profile), [])
        with self.db:
            store.record_failure(self.db, "https://example.com/1", "exit 1", "t")
        [item] = store.deck(self.db, self.profile)
        self.assertTrue(item["basic"])
        self.assertIsNone(item["card"])
        self.assertEqual(item["reasons"], ["core term 'ai' in title"])
        self.assertEqual(tuple(self.db.execute("SELECT attempts, error FROM cards").fetchone()), (3, "exit 1"))

    def test_success_after_failures_clears_error(self):
        with self.db:
            store.upsert_jobs(self.db, [job(1)], "t")
            store.record_failure(self.db, "https://example.com/1", "timed out", "t")
            store.save_card(self.db, "https://example.com/1", CARD, 2, "t")
        row = self.db.execute("SELECT attempts, error, dropped_claims FROM cards").fetchone()
        self.assertEqual(tuple(row), (2, None, 2))
        [item] = store.deck(self.db, self.profile)
        self.assertFalse(item["basic"])
        self.assertEqual(item["card"]["summary"], "Build agents.")

    def test_deck_order_and_exclusions(self):
        profile = make_profile(self.root / "other", applied_urls=["https://example.com/5?utm=x"],
                               applied_roles=[{"company": "Globex", "title_contains": "platform"}])
        rows = [job(1), job(2, score=9), job(3, usd=7000), job(4, company="Globex", title="Platform Engineer"),
                job(5, score=10), job(6, score=8)]
        with self.db:
            store.upsert_jobs(self.db, rows, "t")
            for n in range(1, 7):
                store.save_card(self.db, f"https://example.com/{n}", CARD, 0, "t")
            store.record_swipe(self.db, "https://example.com/6", "apply", "t")
        self.assertEqual([item["url"] for item in store.deck(self.db, profile)],
                         ["https://example.com/2", "https://example.com/3", "https://example.com/1"])

    def test_compensation_line_priority(self):
        self.assertEqual(store.compensation_line("USD 90,000–120,000 annual", {"compensation": "R$ 20k"}),
                         {"text": "USD 90,000–120,000 annual", "note": ""})
        self.assertEqual(store.compensation_line("", {"compensation": "$8k/month"}), {"text": "$8k/month", "note": "from ad text"})
        self.assertEqual(store.compensation_line("", {"compensation": None}), {"text": "Not stated", "note": ""})
        self.assertEqual(store.compensation_line("", None), {"text": "Not stated", "note": ""})

    def test_swipe_is_idempotent_and_undo_removes_most_recent(self):
        with self.db:
            store.upsert_jobs(self.db, [job(1), job(2)], "t")
            self.assertTrue(store.record_swipe(self.db, "https://example.com/1", "apply", "2026-09-29T10:00:00.000001+00:00"))
            self.assertTrue(store.record_swipe(self.db, "https://example.com/1", "apply", "2026-09-29T10:00:01.000000+00:00"))
            store.record_swipe(self.db, "https://example.com/2", "pass", "2026-09-29T10:00:02.000000+00:00")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM swipes").fetchone()[0], 2)
        with self.db:
            self.assertEqual(store.undo_last(self.db), "https://example.com/2")
            self.assertEqual(store.undo_last(self.db), "https://example.com/1")
            self.assertIsNone(store.undo_last(self.db))

    def test_unknown_url_or_decision_is_rejected(self):
        self.assertFalse(store.record_swipe(self.db, "https://example.com/nope", "pass", "t"))
        with self.assertRaises(ValueError):
            store.record_swipe(self.db, "https://example.com/nope", "maybe", "t")

    def test_applies_lists_recent_apply_swipes_only(self):
        with self.db:
            store.upsert_jobs(self.db, [job(1), job(2), job(3)], "t")
            store.record_swipe(self.db, "https://example.com/1", "apply", "2026-09-01T00:00:00.000000+00:00")
            store.record_swipe(self.db, "https://example.com/2", "apply", "2026-09-29T00:00:00.000000+00:00")
            store.record_swipe(self.db, "https://example.com/3", "pass", "2026-09-29T00:00:00.000000+00:00")
        self.assertEqual([a["url"] for a in store.applies(self.db, "2026-09-22T00:00:00+00:00")], ["https://example.com/2"])


if __name__ == "__main__":
    unittest.main()
