"""The profile terms must classify exactly like the regexes they replaced."""

import re
import unittest

from helpers import example
from radar import EXCLUDED, Job, classify

TITLE = re.compile(r"\b(ai|llm|agent|machine learning|full.?stack|back.?end|platform|software engineer)\b", re.I)
NEGATIVE_TITLE = re.compile(r"\b(intern|internship|junior|staff|principal|director|manager|recruiter|designer)\b", re.I)
WRONG_SPECIALTY = re.compile(r"\b(inference optimization|computer vision|embedded|multilingual data|data scientist|security engineer|java developer)\b", re.I)
AI = re.compile(r"\b(ai|llm|agents?|rag|gen.?ai|langgraph|language models?)\b", re.I)
BACKEND = re.compile(r"\b(python|fastapi|django|flask|api|postgres|mongodb|redis|backend|back.end)\b", re.I)
PRODUCT = re.compile(r"\b(react|typescript|full.?stack|product|frontend|front.end)\b", re.I)


def legacy(job, min_usd=5000):
    if not job.url.startswith("https://") or not TITLE.search(job.title) or NEGATIVE_TITLE.search(job.title) or WRONG_SPECIALTY.search(job.title):
        return "excluded", 0
    if job.remote is False or EXCLUDED.search(job.location):
        return "excluded", 0
    if job.usd_month_min is not None and job.usd_month_min < min_usd:
        return "excluded", 0
    score = 0
    for pattern, points in [(AI, 4), (BACKEND, 3), (PRODUCT, 2)]:
        if pattern.search(job.title):
            score += points
        elif pattern.search(job.description):
            score += 1
    if job.remote is True:
        score += 1
    if job.usd_month_min is not None:
        score += 2
    return ("review" if score >= 5 else "low-fit"), score


FIXTURES = [
    ("Senior AI Engineer", "Remote, Brazil", "Python LLM agents", True, None),
    ("Full-Stack Engineer", "LATAM", "React TypeScript product", True, 7000),
    ("Backend Engineer", "Worldwide", "FastAPI Postgres Redis", None, None),
    ("Software Engineer, Platform", "Americas", "Kubernetes and Terraform", None, None),
    ("Staff AI Engineer", "Remote", "LLM agents", True, None),
    ("Junior Backend Developer", "Remote", "Python", True, None),
    ("Machine Learning Engineer, Computer Vision", "Worldwide", "PyTorch", True, None),
    ("AI Engineer", "US only", "Python LLM agents", True, None),
    ("LLM Engineer", "Hybrid - São Paulo", "RAG pipelines", None, None),
    ("Agent Engineer", "Remote", "Build RAG and GenAI tools with LangGraph", True, 4000),
    ("Product Engineer (Fullstack)", "Brasil", "frontend in react", None, None),
    ("Data Engineer", "Remote", "Python and Spark", True, None),
    ("Senior Backend Engineer", "Brasil", "Django, Flask and MongoDB; front-end in React", False, None),
    ("AI Solutions Architect", "Global", "large language models and generative AI", True, None),
    ("Back-end Engineer", "Anywhere", "API design", None, None),
    ("Front End Platform Engineer", "Remote", "front-end, typescript", True, 9000),
    ("Security Engineer, AI", "Remote", "LLM security", True, None),
    ("Engineering Manager, AI", "Remote", "lead agents team", True, None),
    ("Software Engineer", "Remote - LATAM", "no keywords here", True, None),
    ("Platform Engineer", "Latin America", "language model infra, gen-ai", None, 6000),
]


class RegressionTests(unittest.TestCase):
    def check(self, profile):
        for n, (title, location, description, remote, usd) in enumerate(FIXTURES):
            job = Job("Example", title, location, f"https://example.com/{n}", description, "test", usd_month_min=usd, remote=remote)
            status, score, _ = classify(job, profile)
            self.assertEqual((status, score), legacy(job), f"{profile.name}: {title} / {location}")

    def test_example_profile_matches_legacy_regexes(self):
        self.check(example())


if __name__ == "__main__":
    unittest.main()
