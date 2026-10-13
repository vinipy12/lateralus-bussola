"""Tripwire for Codex upgrades: one fixture ad through real `codex exec`. Run with BUSSOLA_LIVE_CODEX=1."""

import os
import unittest

import enrich
from helpers import example

AD = {"url": "https://example.com/jobs/live", "company": "Example Labs", "title": "Senior AI Engineer",
      "location": "Remote - LATAM (Brazil, Argentina, Mexico)", "salary": "",
      "description": ("We are hiring a Senior AI Engineer to build LLM agents and retrieval pipelines in Python. "
                      "You must overlap at least 4 hours with US Eastern time. Compensation: USD 7,000 - 9,000 per month. "
                      "Requirements: 5+ years of Python, production experience with RAG, strong English. "
                      "We hire contractors anywhere in Latin America, including Brazil.")}


@unittest.skipUnless(os.environ.get("BUSSOLA_LIVE_CODEX") == "1", "set BUSSOLA_LIVE_CODEX=1 to call real Codex")
class LiveCodexTest(unittest.TestCase):
    def test_fixture_ad_returns_a_schema_valid_grounded_card(self):
        card, dropped = enrich.enrich_job(AD, example())
        self.assertIn(card["eligible"], enrich.VERDICTS)
        self.assertTrue(card["summary"])
        self.assertLessEqual(len(card["must_haves"]), 5)
        for claim in card["pros"] + card["cons"]:
            self.assertTrue(enrich.supported(claim["quote"], AD), claim)
        print(f"\nlive card: eligible={card['eligible']} comp={card['compensation']!r} dropped={dropped}")


if __name__ == "__main__":
    unittest.main()
