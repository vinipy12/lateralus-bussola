import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import store
from helpers import example, make_profile, write_profile
from radar import Job, classify, fetch, main, parse_ashby, parse_higlobe, parse_himalayas, run

EXAMPLE = example()


def ashby(*ids):
    return {"jobs": [{"title": f"Senior AI Engineer {i}", "location": "Remote, Brazil", "jobUrl": f"https://example.com/{i}",
                      "descriptionPlain": "Python LLM agents", "isRemote": True} for i in ids]}


def higlobe_item(job_id, title, location, company, body="<p>Python LLM agents</p>"):
    escaped = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<item><title>{title}</title><link>https://jobs.higlobe.com/job/{job_id}/x/</link>
        <description>
                {location}
                {company}
                {escaped}
        </description>
        <pubDate>Mon, 12 Oct 2026 07:02:16 -0400</pubDate>
        <guid>https://jobs.higlobe.com/job/{job_id}/x/</guid><dc:creator>{company}</dc:creator></item>"""


def higlobe(*items):
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<channel><title>SmartJobBoard</title>" + "".join(items) + "</channel></rss>")


class RadarTests(unittest.TestCase):
    def test_ashby_salary_and_unlisted_jobs(self):
        data = {"jobs": [
            {"title": "AI Agent Engineer", "location": "Remote", "jobUrl": "https://jobs.ashbyhq.com/example/1", "isListed": True,
             "isRemote": True, "compensation": {"summaryComponents": [{"compensationType": "Salary", "currencyCode": "USD", "interval": "1 YEAR", "minValue": 78000}]}},
            {"title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/example/2", "isListed": False},
        ]}
        parsed = parse_ashby("Example", data)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].usd_month_min, 6500)

    def test_under_floor_is_excluded_and_unknown_salary_is_flagged(self):
        base = dict(company="Example", title="Senior AI Engineer", location="Brazil", url="https://example.com/job",
                    description="Build LLM agents with Python", source="test", remote=True)
        self.assertEqual(classify(Job(**base, usd_month_min=4900), EXAMPLE)[0], "excluded")
        status, _, reasons = classify(Job(**base), EXAMPLE)
        self.assertEqual(status, "review")
        self.assertIn("salary unverified", reasons)

    def test_remote_us_only_and_irrelevant_specialty_are_excluded(self):
        base = dict(company="Example", location="US only", url="https://example.com/job",
                    description="Python LLM agents", source="test", remote=True)
        self.assertEqual(classify(Job(title="AI Engineer", **base), EXAMPLE)[0], "excluded")
        base["location"] = "Worldwide"
        self.assertEqual(classify(Job(title="Senior Machine Learning Engineer, Inference Optimization", **base), EXAMPLE)[0], "excluded")

    def test_himalayas_monthly_salary_and_attribution(self):
        parsed = parse_himalayas({"jobs": [{"title": "Backend Engineer", "companyName": "Example", "locationRestrictions": ["Brazil"],
            "minSalary": 6000, "maxSalary": 8000, "salaryPeriod": "monthly", "currency": "USD", "applicationLink": "https://himalayas.app/jobs/1"}]})
        self.assertEqual(parsed[0].usd_month_min, 6000)
        self.assertEqual(parsed[0].source, "Himalayas")

    def test_one_feed_timeout_does_not_erase_successful_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root / "profiles", search_queries=[], sources=[
                {"type": "greenhouse", "board": "first", "company": "First"},
                {"type": "ashby", "board": "second", "company": "Second"}])
            successful = {"jobs": [{"title": "Senior AI Engineer", "location": "Remote, Brazil",
                         "jobUrl": "https://example.com/job", "descriptionPlain": "Python LLM agents", "isRemote": True}]}
            with patch("radar.get_json", side_effect=[TimeoutError("slow feed"), successful]):
                count, matches = run(profile, root / "out" / "example")
            report = (root / "out" / "example" / "shortlist.md").read_text()
            self.assertEqual((count, matches), (1, 1))
            self.assertIn("First: TimeoutError", report)
            self.assertIn("Second — Senior AI Engineer", report)

    def test_unrestricted_himalayas_listing_keeps_brazil_flag(self):
        parsed = parse_himalayas({"jobs": [{"title": "Senior AI Engineer", "companyName": "Example",
            "description": "Python LLM agents", "applicationLink": "https://himalayas.app/jobs/2"}]})
        status, _, reasons = classify(parsed[0], EXAMPLE)
        self.assertEqual(status, "review")
        self.assertIn("Brazil eligibility unverified", reasons)

    def test_ashby_hourly_salary_converts_and_unknown_interval_is_unverified(self):
        def floor(interval):
            data = {"jobs": [{"title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/example/1", "compensation": {"summaryComponents": [
                {"compensationType": "Salary", "currencyCode": "USD", "interval": interval, "minValue": 50}]}}]}
            return parse_ashby("Example", data)[0].usd_month_min
        self.assertAlmostEqual(floor("1 HOUR"), 50 * 40 * 52 / 12)
        self.assertIsNone(floor("1 FORTNIGHT"))

    def test_remote_bonus_is_explained(self):
        job = Job("Example", "Senior AI Engineer", "Brazil", "https://example.com/job", "Python LLM agents", "test", remote=True)
        self.assertIn("remote", classify(job, EXAMPLE)[2])

    def test_second_run_marks_only_new_jobs_and_lists_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root / "profiles", max_results=1, search_queries=[],
                                   sources=[{"type": "ashby", "board": "only", "company": "Only"}])
            out = root / "out" / "example"
            with patch("radar.get_json", return_value=ashby(1, 2)):
                run(profile, out)
            with patch("radar.get_json", return_value=ashby(1, 2, 3)):
                run(profile, out)
            report = (out / "shortlist.md").read_text()
            self.assertIn("new since last run: 1", report)
            self.assertEqual(report.count("— NEW"), 1)
            self.assertIn("Also matched (2 below the cut)", report)
            self.assertEqual(len(list((out / "history").glob("*.md"))), 1)
            with closing(store.connect(out / "radar.db")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)

    def test_remote_only_false_keeps_hybrid_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), remote_only=False)
        job = Job("Example", "Senior AI Engineer", "Hybrid - Lisbon", "https://example.com/job", "Python LLM agents", "test", remote=False)
        self.assertEqual(classify(job, EXAMPLE)[0], "excluded")
        self.assertEqual(classify(job, profile)[0], "review")

    def test_himalayas_search_uses_profile_country(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), country="AR", search_queries=["python"], sources=[])
        with patch("radar.get_json", return_value={"jobs": []}) as feed:
            fetch(profile)
        self.assertIn("country=AR", feed.call_args.args[0])

    def test_applied_jobs_are_left_out_of_the_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = make_profile(root / "profiles", search_queries=[], applied_urls=["https://example.com/1"],
                                   sources=[{"type": "ashby", "board": "only", "company": "Only"}])
            with patch("radar.get_json", return_value=ashby(1, 2)):
                self.assertEqual(run(profile, root / "out" / "example"), (2, 1))

    def test_all_active_skips_inactive_and_survives_a_broken_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            feed = [{"type": "ashby", "board": "only", "company": "Only"}]
            write_profile(profiles, "alice", active=True, search_queries=[], sources=feed)
            write_profile(profiles, "bob", active=False, search_queries=[], sources=feed)
            write_profile(profiles, "carol", active=True, min_usd_month="lots")
            err = io.StringIO()
            with patch("radar.get_json", return_value=ashby(1)) as get, redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["--all-active", "--profiles-dir", str(profiles), "--out-dir", str(root / "out")])
            self.assertEqual(code, 0)
            self.assertEqual(get.call_count, 1)
            self.assertTrue((root / "out" / "alice" / "shortlist.md").exists())
            self.assertFalse((root / "out" / "bob").exists())
            self.assertIn("carol", err.getvalue())
            self.assertIn("min_usd_month", err.getvalue())

    def test_higlobe_rss_item_becomes_a_job(self):
        parsed = parse_higlobe(higlobe(higlobe_item(12345, "AI Engineer", "Remote (Latin America)", "Globex Consulting",
                                                    "<h2>About</h2><p>Build LLM &amp; agent pipelines</p>")))
        self.assertEqual(len(parsed), 1)
        job = parsed[0]
        self.assertEqual((job.company, job.title, job.location), ("Globex Consulting", "AI Engineer", "Remote (Latin America)"))
        self.assertEqual(job.url, "https://jobs.higlobe.com/job/12345/x/")
        self.assertEqual((job.source, job.published, job.remote), ("HiGlobe", "2026-10-12", True))
        self.assertEqual(job.description, "About  Build LLM & agent pipelines")

    def test_higlobe_office_only_location_is_not_remote(self):
        parsed = parse_higlobe(higlobe(higlobe_item(1, "AI Engineer", "Singapore", "Initech"),
                                       higlobe_item(2, "AI Engineer", "Bangalore, India; Remote, Poland", "Initech")))
        self.assertEqual([job.remote for job in parsed], [False, True])
        self.assertEqual(classify(parsed[0], EXAMPLE)[0], "excluded")

    def test_higlobe_item_without_header_lines_keeps_its_body(self):
        item = higlobe_item(3, "Python Engineer", "", "", "<p>Acme builds LLM agents</p>").replace("<dc:creator></dc:creator>", "")
        job = parse_higlobe(higlobe(item))[0]
        self.assertEqual((job.location, job.company, job.remote), ("", "", None))
        self.assertEqual(job.description, "Acme builds LLM agents")

    def test_higlobe_source_reads_the_board_rss_and_survives_a_broken_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), search_queries=[], sources=[
                {"type": "higlobe", "board": "jobs", "company": "HiGlobe"}])
        with patch("radar.get_text", return_value=higlobe(higlobe_item(1, "AI Engineer", "Remote (Worldwide)", "Acme"))) as feed:
            jobs, errors = fetch(profile)
        self.assertEqual(feed.call_args.args[0], "https://jobs.higlobe.com/rss/")
        self.assertEqual(([job.company for job in jobs.values()], errors), (["Acme"], []))
        with patch("radar.get_text", return_value="<rss><channel>"):
            jobs, errors = fetch(profile)
        self.assertEqual(jobs, {})
        self.assertIn("HiGlobe: ParseError", errors[0])

    def test_employer_posting_wins_over_higlobe_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), search_queries=[], sources=[
                {"type": "higlobe", "board": "jobs", "company": "HiGlobe"},
                {"type": "ashby", "board": "acme", "company": "Acme"}])
        listing = higlobe(higlobe_item(1, "Senior AI Engineer 7", "Remote (Worldwide)", "Acme"))
        with patch("radar.get_text", return_value=listing), patch("radar.get_json", return_value=ashby(7)):
            jobs, _ = fetch(profile)
        self.assertEqual([job.source for job in jobs.values()], ["Ashby"])


if __name__ == "__main__":
    unittest.main()
