import io
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import enrich
import store
from codex_client import CodexError
from helpers import codex_subprocess, make_profile, write_profile
from radar import Job

JOB = {"url": "https://example.com/1", "company": "Acme", "title": "Senior AI Engineer", "location": "Remote — LATAM",
       "salary": "", "description": "We hire in  Brazil\nand Mexico.\nYou will build LLM agents in Python. Pay: $8,000/month."}


def card(**overrides):
    base = {"summary": "Build LLM agents.", "eligible": "yes", "eligible_quote": "we hire in brazil", "timezone": None,
            "compensation": "$8,000/month", "seniority": "Senior", "must_haves": ["Python"],
            "pros": [{"text": "Agents are the job", "quote": "build LLM agents"}],
            "cons": [], "dealbreaker": None, "dealbreaker_quote": None}
    base.update(overrides)
    return base


class GroundingTests(unittest.TestCase):
    def test_supported_ignores_whitespace_and_case(self):
        self.assertTrue(enrich.supported("WE HIRE IN BRAZIL AND MEXICO", JOB))
        self.assertTrue(enrich.supported("senior ai engineer", JOB))
        self.assertTrue(enrich.supported("latam", JOB))
        self.assertFalse(enrich.supported("we hire worldwide", JOB))
        self.assertFalse(enrich.supported("", JOB))
        self.assertFalse(enrich.supported(None, JOB))

    def test_unsupported_claims_are_dropped_and_counted(self):
        grounded, dropped = enrich.ground(card(
            pros=[{"text": "Agents", "quote": "build LLM agents"}, {"text": "Equity", "quote": "generous equity"}],
            cons=[{"text": "Unpaid", "quote": "volunteer role"}]), JOB)
        self.assertEqual([p["text"] for p in grounded["pros"]], ["Agents"])
        self.assertEqual(grounded["cons"], [])
        self.assertEqual(dropped, 2)

    def test_unsupported_eligibility_becomes_unclear(self):
        grounded, dropped = enrich.ground(card(eligible="no", eligible_quote="US residents only"), JOB)
        self.assertEqual((grounded["eligible"], grounded["eligible_quote"], dropped), ("unclear", None, 0))

    def test_unsupported_dealbreaker_is_dropped(self):
        grounded, dropped = enrich.ground(card(dealbreaker="On-site in NYC", dealbreaker_quote="on-site in New York"), JOB)
        self.assertEqual((grounded["dealbreaker"], grounded["dealbreaker_quote"], dropped), (None, None, 1))
        kept, dropped = enrich.ground(card(dealbreaker="Mexico only?", dealbreaker_quote="Brazil and Mexico"), JOB)
        self.assertEqual((kept["dealbreaker"], dropped), ("Mexico only?", 0))


class ShapeTests(unittest.TestCase):
    def test_bad_verdict_or_types_raise(self):
        with self.assertRaises(CodexError):
            enrich.check_shape(card(eligible="maybe"))
        with self.assertRaises(CodexError):
            enrich.check_shape(card(pros=[{"text": "x"}]))
        with self.assertRaises(CodexError):
            enrich.check_shape(card(summary=None))

    def test_lists_are_truncated_and_blank_strings_become_null(self):
        shaped = enrich.check_shape(card(must_haves=[str(n) for n in range(8)], timezone="  ",
                                         cons=[{"text": str(n), "quote": "q"} for n in range(5)]))
        self.assertEqual(len(shaped["must_haves"]), 5)
        self.assertEqual(len(shaped["cons"]), 3)
        self.assertIsNone(shaped["timezone"])

    def test_prompt_marks_ad_as_untrusted_and_carries_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), fit_profile="Python agent builder", min_usd_month=6000)
        prompt = enrich.build_prompt(JOB, profile)
        self.assertIn("<job_ad>", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("Python agent builder", prompt)
        self.assertIn("6,000", prompt)
        self.assertIn("Brazil", prompt)


class EnrichProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = make_profile(self.root / "profiles")
        self.out = self.root / "out" / "example"
        with closing(store.connect(self.out / "radar.db")) as db, db:
            store.upsert_jobs(db, [(7, Job("Acme", JOB["title"], JOB["location"], JOB["url"], JOB["description"], "Ashby"), ["r"]),
                                   (6, Job("Beta", "Backend Engineer", "Remote", "https://example.com/2", "Python APIs", "Ashby"), ["r"])], "t")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cards_saved_through_the_subprocess_boundary(self):
        def responder(prompt):
            return card() if "Senior AI Engineer" in prompt else 1
        fake, calls = codex_subprocess(responder)
        with patch("codex_client.subprocess.run", fake), redirect_stderr(io.StringIO()):
            self.assertEqual(enrich.enrich_profile(self.profile, self.out), (1, 1))
        self.assertEqual(len(calls), 2)
        with closing(store.connect(self.out / "radar.db")) as db:
            [item] = store.deck(db, self.profile)
            self.assertEqual(item["card"]["pros"][0]["quote"], "build LLM agents")
            self.assertEqual(item["compensation"], {"text": "$8,000/month", "note": "from ad text"})
            self.assertEqual(len(store.pending_jobs(db)), 1)

    def test_three_failures_become_a_basic_card_and_stop_retrying(self):
        def failing(job, profile, runner):
            raise CodexError("timed out after 120s")
        with patch("enrich.enrich_job", failing), redirect_stderr(io.StringIO()):
            for _ in range(3):
                enrich.enrich_profile(self.profile, self.out)
            self.assertEqual(enrich.enrich_profile(self.profile, self.out), (0, 0))
        with closing(store.connect(self.out / "radar.db")) as db:
            self.assertTrue(all(item["basic"] for item in store.deck(db, self.profile)))

    def test_unavailable_codex_skips_run_and_notes_shortlist(self):
        write_profile(self.root / "profiles", "example", active=True)
        (self.out / "shortlist.md").write_text("# Career radar\n")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = enrich.main(["--all-active", "--profiles-dir", str(self.root / "profiles"), "--out-dir", str(self.root / "out")],
                               check=lambda: "codex is not logged in (run `codex login`)")
        self.assertEqual(code, 1)
        self.assertIn("AI cards skipped this run: codex is not logged in", (self.out / "shortlist.md").read_text())

    def test_unexpected_worker_error_is_recorded_and_others_still_run(self):
        def mixed(job, profile, runner):
            if job["url"].endswith("/1"):
                raise TypeError("boom")
            return card(), 0
        with patch("enrich.enrich_job", mixed), redirect_stderr(io.StringIO()):
            self.assertEqual(enrich.enrich_profile(self.profile, self.out), (1, 1))
        with closing(store.connect(self.out / "radar.db")) as db:
            cards = store.deck(db, self.profile)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["url"], "https://example.com/2")
            self.assertIsNotNone(cards[0]["card"])
            pending = store.pending_jobs(db)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["url"], "https://example.com/1")

    def test_braces_in_ad_text_do_not_break_the_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), fit_profile="{x} placeholder", min_usd_month=5000)
        job_with_braces = {"url": "https://example.com/3", "company": "Acme", "title": "Engineer {summary}",
                           "location": "Remote", "salary": "{0} base", "description": "Work with {} frameworks"}
        prompt = enrich.build_prompt(job_with_braces, profile)
        self.assertIn("{x} placeholder", prompt)
        self.assertIn("Engineer {summary}", prompt)
        self.assertIn("{0} base", prompt)
        self.assertIn("Work with {} frameworks", prompt)


if __name__ == "__main__":
    unittest.main()
