import json
import tempfile
import unittest
from pathlib import Path

from helpers import example, make_profile, write_profile
from profiles import ProfileError, compile_terms, is_applied, list_profiles, load_profile


class CompileTermsTests(unittest.TestCase):
    def test_space_or_hyphen_matches_space_hyphen_or_nothing(self):
        pattern = compile_terms(["full stack"])
        for text in ["Full Stack Engineer", "full-stack", "FULLSTACK dev"]:
            self.assertTrue(pattern.search(text), text)
        self.assertFalse(pattern.search("fully stacked"))

    def test_word_boundaries_and_escaping(self):
        self.assertFalse(compile_terms(["ai"]).search("maintain"))
        self.assertTrue(compile_terms(["ai"]).search("Senior AI Engineer"))
        self.assertTrue(compile_terms(["node.js"]).search("Node.js backend"))
        self.assertFalse(compile_terms(["node.js"]).search("nodexjs"))

    def test_terms_starting_or_ending_with_a_symbol_match(self):
        self.assertTrue(compile_terms(["c++"]).search("Senior C++ Developer"))
        self.assertTrue(compile_terms(["c#"]).search("C# / .NET engineer"))
        self.assertTrue(compile_terms([".net"]).search("C# / .NET engineer"))
        self.assertTrue(compile_terms([".net"]).search(".NET"))
        self.assertFalse(compile_terms(["c++"]).search("abc++"))
        self.assertFalse(compile_terms(["c#"]).search("c#x"))

    def test_empty_list_never_matches(self):
        self.assertFalse(compile_terms([]).search("anything at all"))
        self.assertFalse(compile_terms(["  "]).search(" "))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertFieldError(self, field, **overrides):
        with self.assertRaises(ProfileError) as caught:
            make_profile(self.dir, **overrides)
        self.assertIn(field, str(caught.exception))

    def test_example_profile_is_valid(self):
        profile = example()
        self.assertEqual((profile.name, profile.country_label, profile.min_usd_month), ("example", "Brazil", 5000))

    def test_min_usd_month_must_be_a_number(self):
        self.assertFieldError("min_usd_month", min_usd_month="5000")
        self.assertFieldError("min_usd_month", min_usd_month=True)

    def test_missing_field_is_named(self):
        root = write_profile(self.dir)
        raw = json.loads((root / "profile.json").read_text())
        del raw["country"]
        (root / "profile.json").write_text(json.dumps(raw))
        with self.assertRaises(ProfileError) as caught:
            load_profile(root)
        self.assertIn("country", str(caught.exception))

    def test_title_include_must_not_be_empty(self):
        self.assertFieldError("title_include", title_include=[])

    def test_types_are_checked(self):
        self.assertFieldError("remote_only", remote_only="yes")
        self.assertFieldError("country", country="Brazil")
        self.assertFieldError("core_terms", core_terms="ai")
        self.assertFieldError("sources", sources=[{"type": "ashby"}])

    def test_name_must_match_directory(self):
        root = write_profile(self.dir)
        raw = json.loads((root / "profile.json").read_text())
        raw["name"] = "someone-else"
        (root / "profile.json").write_text(json.dumps(raw))
        with self.assertRaises(ProfileError) as caught:
            load_profile(root)
        self.assertIn("name", str(caught.exception))

    def test_invalid_json_is_reported(self):
        root = self.dir / "broken"
        root.mkdir()
        (root / "profile.json").write_text("{not json")
        with self.assertRaises(ProfileError):
            load_profile(root)

    def test_list_profiles_skips_bad_names_and_missing_files(self):
        write_profile(self.dir, "alice")
        (self.dir / "empty").mkdir()
        (self.dir / "Bad Name").mkdir()
        (self.dir / "Bad Name" / "profile.json").write_text("{}")
        self.assertEqual(list_profiles(self.dir), ["alice"])
        self.assertEqual(list_profiles(self.dir / "missing"), [])


class AppliedTests(unittest.TestCase):
    def test_applied_by_url_ignoring_query_and_by_role(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(Path(directory), applied_urls=["https://example.com/jobs/1/"],
                                   applied_roles=[{"company": "Initech", "title_contains": "Payments"}])
        self.assertTrue(is_applied(profile, "https://example.com/jobs/1?utm=x", "Other", "Anything"))
        self.assertTrue(is_applied(profile, "https://x.com/2", "Initech Inc", "Engineer, Payments"))
        self.assertFalse(is_applied(profile, "https://x.com/3", "Initech", "Engineer, Search"))


if __name__ == "__main__":
    unittest.main()
