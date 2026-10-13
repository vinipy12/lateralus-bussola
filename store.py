"""Each profile's radar.db: seen jobs, the review queue, AI cards and swipes. Shared by the host jobs and the swipe container."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from profiles import Profile, is_applied

MAX_ATTEMPTS = 3
DECISIONS = ("pass", "apply")
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    url TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    url TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL,
    salary TEXT NOT NULL, usd_month_min REAL, source TEXT NOT NULL, published TEXT NOT NULL,
    description TEXT NOT NULL, score INTEGER NOT NULL, reasons_json TEXT NOT NULL, queued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards (
    url TEXT PRIMARY KEY, card_json TEXT, dropped_claims INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS swipes (
    url TEXT PRIMARY KEY, decision TEXT NOT NULL CHECK (decision IN ('pass', 'apply')), swiped_at TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    return db


def seen_urls(db: sqlite3.Connection) -> set[str]:
    return {row["url"] for row in db.execute("SELECT url FROM seen_jobs")}


def record_seen(db: sqlite3.Connection, jobs, stamp: str) -> None:
    db.executemany("INSERT INTO seen_jobs VALUES (?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                   "company = excluded.company, title = excluded.title, last_seen = excluded.last_seen",
                   [(job.url, job.company, job.title, stamp, stamp) for job in jobs])


def upsert_jobs(db: sqlite3.Connection, ranked, stamp: str) -> None:
    # queued_at keeps the first time a job entered the review queue.
    db.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
        "company = excluded.company, title = excluded.title, location = excluded.location, salary = excluded.salary, "
        "usd_month_min = excluded.usd_month_min, source = excluded.source, published = excluded.published, "
        "description = excluded.description, score = excluded.score, reasons_json = excluded.reasons_json",
        [(job.url, job.company, job.title, job.location, job.salary, job.usd_month_min, job.source, job.published,
          job.description, score, json.dumps(reasons), stamp) for score, job, reasons in ranked])


def pending_jobs(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT j.* FROM jobs j LEFT JOIN cards c ON c.url = j.url "
                      "WHERE c.card_json IS NULL AND COALESCE(c.attempts, 0) < ? "
                      "AND j.url NOT IN (SELECT url FROM swipes) ORDER BY j.score DESC", (MAX_ATTEMPTS,))
    return [dict(row) for row in rows]


def save_card(db: sqlite3.Connection, url: str, card: dict, dropped: int, stamp: str) -> None:
    db.execute("INSERT INTO cards (url, card_json, dropped_claims, attempts, error, created_at) VALUES (?, ?, ?, 1, NULL, ?) "
               "ON CONFLICT(url) DO UPDATE SET card_json = excluded.card_json, dropped_claims = excluded.dropped_claims, "
               "attempts = cards.attempts + 1, error = NULL, created_at = excluded.created_at",
               (url, json.dumps(card), dropped, stamp))


def record_failure(db: sqlite3.Connection, url: str, error: str, stamp: str) -> None:
    db.execute("INSERT INTO cards (url, card_json, dropped_claims, attempts, error, created_at) VALUES (?, NULL, 0, 1, ?, ?) "
               "ON CONFLICT(url) DO UPDATE SET attempts = cards.attempts + 1, error = excluded.error",
               (url, error[:500], stamp))


def compensation_line(salary: str, card: dict | None) -> dict:
    if salary:
        return {"text": salary, "note": ""}
    if card and card.get("compensation"):
        return {"text": card["compensation"], "note": "from ad text"}
    return {"text": "Not stated", "note": ""}


def deck(db: sqlite3.Connection, profile: Profile) -> list[dict]:
    rows = db.execute("SELECT j.*, c.card_json, c.dropped_claims FROM jobs j JOIN cards c ON c.url = j.url "
                      "WHERE (c.card_json IS NOT NULL OR c.attempts >= ?) AND j.url NOT IN (SELECT url FROM swipes)",
                      (MAX_ATTEMPTS,))
    items = []
    for row in rows:
        if is_applied(profile, row["url"], row["company"], row["title"]):
            continue
        card = json.loads(row["card_json"]) if row["card_json"] else None
        items.append({"url": row["url"], "company": row["company"], "title": row["title"], "location": row["location"],
                      "source": row["source"], "published": row["published"], "score": row["score"],
                      "usd_month_min": row["usd_month_min"], "reasons": json.loads(row["reasons_json"]),
                      "card": card, "basic": card is None, "dropped_claims": row["dropped_claims"],
                      "compensation": compensation_line(row["salary"], card)})
    # Same order as the shortlist.
    items.sort(key=lambda item: (-item["score"], item["usd_month_min"] is None,
                                 item["company"].casefold(), item["title"].casefold()))
    return items


def record_swipe(db: sqlite3.Connection, url: str, decision: str, stamp: str) -> bool:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if db.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone() is None:
        return False
    db.execute("INSERT INTO swipes VALUES (?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
               "decision = excluded.decision, swiped_at = excluded.swiped_at", (url, decision, stamp))
    return True


def undo_last(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT url FROM swipes ORDER BY swiped_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    db.execute("DELETE FROM swipes WHERE url = ?", (row["url"],))
    return row["url"]


def applies(db: sqlite3.Connection, since: str) -> list[dict]:
    rows = db.execute("SELECT s.url, j.company, j.title, s.swiped_at FROM swipes s JOIN jobs j ON j.url = s.url "
                      "WHERE s.decision = 'apply' AND s.swiped_at >= ? ORDER BY s.swiped_at DESC", (since,))
    return [dict(row) for row in rows]
