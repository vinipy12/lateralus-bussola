import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ready
from codex_client import CodexError
from profiles import load_profile

CV = "Maria Souza — Registered Nurse, São Paulo. " * 10
DRAFT = {"search_queries": ["remote nurse"], "title_include": ["nurse", "clinical"], "title_exclude": ["director"],
         "specialty_exclude": ["surgical"], "core_terms": ["nurse"], "adjacent_terms": ["telehealth"], "bonus_terms": ["spanish"],
         "eligible_region_terms": ["brazil", "latam", "worldwide"], "fit_profile": "Registered nurse moving into telehealth."}


def pdftotext(stdout, code=0):
    return lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, code, stdout, "boom")


class ReadyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "cv.pdf"
        self.pdf.write_bytes(b"%PDF-1.4")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, answers, runner=lambda prompt, schema: DRAFT, text=CV, extra=()):
        inputs = iter(answers)
        with patch("ready.subprocess.run", pdftotext(text)), patch("ready.radar.fetch", return_value=({}, [])), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = ready.main(["--name", "maria", "--profiles-dir", str(self.root / "profiles"), *extra, str(self.pdf)],
                              input_fn=lambda prompt: next(inputs), runner=runner, check=lambda: None)
        return code, out.getvalue(), err.getvalue()

    def test_scanned_pdf_is_rejected(self):
        with patch("ready.subprocess.run", pdftotext("   short  ")), self.assertRaises(ready.ReadyError) as caught:
            ready.extract_text(self.pdf)
        self.assertIn("scanned", str(caught.exception))

    def test_happy_path_writes_inactive_profile_and_cv(self):
        code, out, _ = self.run_main(["br", "maybe", "y", "lots", "2,500"])
        self.assertEqual(code, 0)
        profile = load_profile(self.root / "profiles" / "maria")
        self.assertEqual((profile.active, profile.country, profile.remote_only, profile.min_usd_month), (False, "BR", True, 2500))
        raw = json.loads((self.root / "profiles" / "maria" / "profile.json").read_text())
        self.assertEqual((raw["sources"], raw["applied_urls"], raw["applied_roles"]), ([], [], []))
        self.assertEqual((self.root / "profiles" / "maria" / "cv.txt").read_text(), CV.strip())
        self.assertIn("0 jobs fetched, 0 would become cards", out)

    def test_codex_failure_writes_nothing(self):
        def broken(prompt, schema):
            raise CodexError("exit 1")
        code, _, err = self.run_main([], runner=broken)
        self.assertEqual(code, 1)
        self.assertIn("exit 1", err)
        self.assertFalse((self.root / "profiles" / "maria").exists())

    def test_existing_profile_needs_force(self):
        self.assertEqual(self.run_main(["BR", "n", "3000"])[0], 0)
        code, _, err = self.run_main(["BR", "n", "3000"])
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.run_main(["BR", "n", "3000"], extra=["--force"])[0], 0)

    def test_malformed_draft_is_rejected(self):
        code, _, err = self.run_main([], runner=lambda prompt, schema: {**DRAFT, "core_terms": "nurse"})
        self.assertEqual(code, 1)
        self.assertIn("core_terms", err)


if __name__ == "__main__":
    unittest.main()
