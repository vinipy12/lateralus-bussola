#!/usr/bin/env python3
"""Read public ATS job feeds and produce a reviewable personal shortlist."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import store
from profiles import Profile, ProfileError, is_applied, list_profiles, load_profile

HERE = Path(__file__).resolve().parent

PER_MONTH = {"YEAR": 1 / 12, "MONTH": 1, "WEEK": 52 / 12, "HOUR": 40 * 52 / 12}
# Multi-company listing sites; a direct employer posting for the same role is preferred over theirs.
AGGREGATORS = frozenset({"Himalayas", "HiGlobe"})
EXCLUDED = re.compile(r"\b(us only|united states only|canada only|europe only|emea only|eu only|hybrid|on.?site)\b", re.I)



@dataclass(frozen=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    description: str
    source: str
    published: str = ""
    salary: str = ""
    usd_month_min: float | None = None
    remote: bool | None = None


def get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "PersonalCareerRadar/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "PersonalCareerRadar/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def ashby_salary(raw: dict) -> tuple[str, float | None]:
    compensation = raw.get("compensation") or {}
    summary = compensation.get("compensationTierSummary") or compensation.get("scrapeableCompensationSalarySummary") or ""
    values: list[float] = []
    for component in compensation.get("summaryComponents") or []:
        if component.get("compensationType") != "Salary" or component.get("currencyCode") != "USD":
            continue
        value = component.get("minValue")
        interval = str(component.get("interval", "")).upper()
        multiplier = next((m for unit, m in PER_MONTH.items() if unit in interval), None)
        # An unknown interval leaves the floor unverified rather than reading as $0.
        if isinstance(value, (int, float)) and multiplier:
            values.append(value * multiplier)
    return summary, min(values) if values else None


def parse_ashby(company: str, data: dict) -> list[Job]:
    result = []
    for raw in data.get("jobs", []):
        if raw.get("isListed") is False:
            continue
        salary, floor = ashby_salary(raw)
        result.append(Job(company, raw.get("title", ""), raw.get("location", ""), raw.get("jobUrl", ""),
                          plain(raw.get("descriptionPlain") or raw.get("descriptionHtml") or ""), "Ashby",
                          raw.get("publishedAt", ""), salary, floor, raw.get("isRemote")))
    return result


def parse_greenhouse(company: str, data: dict) -> list[Job]:
    return [Job(company, raw.get("title", ""), (raw.get("location") or {}).get("name", ""),
                raw.get("absolute_url", ""), plain(raw.get("content", "")), "Greenhouse",
                raw.get("updated_at", ""), remote=None) for raw in data.get("jobs", [])]


def parse_lever(company: str, data: list[dict]) -> list[Job]:
    result = []
    for raw in data:
        desc = raw.get("descriptionPlain") or raw.get("description") or ""
        result.append(Job(company, raw.get("text", ""), (raw.get("categories") or {}).get("location", ""),
                          raw.get("hostedUrl", ""), plain(desc), "Lever"))
    return result


def parse_himalayas(data: dict) -> list[Job]:
    result = []
    for raw in data.get("jobs", []):
        minimum = raw.get("minSalary")
        period = str(raw.get("salaryPeriod") or "annual").lower()
        currency = str(raw.get("currency") or "").upper()
        multiplier = {"annual": 1 / 12, "monthly": 1, "weekly": 52 / 12, "fortnightly": 26 / 12, "hourly": 40 * 52 / 12}.get(period)
        floor = minimum * multiplier if currency == "USD" and isinstance(minimum, (int, float)) and multiplier else None
        restrictions = raw.get("locationRestrictions") or []
        location = ", ".join(map(str, restrictions)) if restrictions else "No restriction listed (unverified)"
        salary = f"{currency} {minimum:,.0f}–{raw.get('maxSalary', 0):,.0f} {period}" if isinstance(minimum, (int, float)) else ""
        published = raw.get("pubDate")
        if isinstance(published, (int, float)):
            published = datetime.fromtimestamp(published, timezone.utc).date().isoformat()
        result.append(Job(raw.get("companyName", ""), raw.get("title", ""), location,
                          raw.get("applicationLink", ""), plain(raw.get("description", "")), "Himalayas",
                          str(published or ""), salary, floor, True))
    return result


def parse_higlobe(feed: str) -> list[Job]:
    result = []
    for item in ET.fromstring(feed).iter("item"):
        # The feed usually puts a location line and a company line ahead of the HTML body; some items omit both.
        lines = [line.strip() for line in (item.findtext("description") or "").strip().splitlines()]
        header = 2 if len(lines) > 2 and not lines[0].startswith("<") and not lines[1].startswith("<") else 0
        location = lines[0] if header else ""
        company = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or (lines[1] if header else "")
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            published = ""
        result.append(Job(company.strip(), (item.findtext("title") or "").strip(), location,
                          (item.findtext("link") or "").strip(), plain("\n".join(lines[header:])), "HiGlobe",
                          published, remote=bool(re.search(r"\bremote\b", location, re.I)) if location else None))
    return result


def classify(job: Job, profile: Profile) -> tuple[str, int, list[str]]:
    location = job.location
    if (not job.url.startswith("https://") or not profile.title_include.search(job.title)
            or profile.title_exclude.search(job.title) or profile.specialty_exclude.search(job.title)):
        return "excluded", 0, ["outside role scope or invalid URL"]
    if profile.remote_only and (job.remote is False or EXCLUDED.search(location)):
        return "excluded", 0, [f"not clearly compatible with remote work from {profile.country_label}"]
    if job.usd_month_min is not None and job.usd_month_min < profile.min_usd_month:
        return "excluded", 0, [f"published USD floor ${job.usd_month_min:,.0f}/month is below target"]
    score = 0
    reasons = []
    for pattern, points, label in [(profile.core_terms, 4, "core"), (profile.adjacent_terms, 3, "adjacent"), (profile.bonus_terms, 2, "bonus")]:
        if match := pattern.search(job.title):
            score += points
            reasons.append(f"{label} term '{match.group(0).lower()}' in title")
        elif match := pattern.search(job.description):
            score += 1
            reasons.append(f"{label} term '{match.group(0).lower()}' in description")
    if job.remote is True:
        score += 1
        reasons.append("remote")
    if job.usd_month_min is not None:
        score += 2
        reasons.append(f"published USD floor ${job.usd_month_min:,.0f}/month")
    else:
        reasons.append("salary unverified")
    if not profile.eligible_region.search(location):
        reasons.append(f"{profile.country_label} eligibility unverified")
    if job.remote is None and not re.search(r"\bremote\b", location, re.I):
        reasons.append("remote status unverified")
    return ("review" if score >= 5 else "low-fit"), score, reasons


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def fetch(profile: Profile) -> tuple[dict[str, Job], list[str]]:
    jobs: dict[str, Job] = {}
    errors = []
    for source in profile.sources:
        kind, board, company = source["type"], source["board"], source["company"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", board):
            errors.append(f"{company}: invalid board ID")
            continue
        try:
            if kind == "ashby":
                data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")
                parsed = parse_ashby(company, data)
            elif kind == "greenhouse":
                data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
                parsed = parse_greenhouse(company, data)
            elif kind == "lever":
                data = get_json(f"https://api.lever.co/v0/postings/{board}?mode=json")
                parsed = parse_lever(company, data)
            elif kind == "higlobe":
                parsed = parse_higlobe(get_text(f"https://{board}.higlobe.com/rss/"))
            else:
                errors.append(f"{company}: unsupported source type {kind}")
                continue
            for job in parsed:
                if job.url.startswith("https://"):
                    jobs[job.url.split("?")[0].rstrip("/")] = job
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
            errors.append(f"{company}: {type(exc).__name__}: {exc}")

    for query in profile.search_queries:
        try:
            params = urllib.parse.urlencode({"q": query, "country": profile.country, "sort": "recent", "page": 1})
            data = get_json(f"https://himalayas.app/jobs/api/search?{params}")
            for job in parse_himalayas(data):
                if job.url.startswith("https://"):
                    jobs[job.url.split("?")[0].rstrip("/")] = job
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Himalayas {query}: {type(exc).__name__}: {exc}")

    # Prefer the direct employer posting when both ATS and aggregator list the role.
    deduped: dict[tuple[str, str], Job] = {}
    for job in jobs.values():
        key = (job.company.casefold().strip(), job.title.casefold().strip())
        if key not in deduped or (deduped[key].source in AGGREGATORS and job.source not in AGGREGATORS):
            deduped[key] = job
    return {job.url: job for job in deduped.values()}, errors


def rank(jobs: dict[str, Job], profile: Profile) -> list[tuple[int, Job, list[str]]]:
    ranked = []
    for job in jobs.values():
        if is_applied(profile, job.url, job.company, job.title):
            continue
        status, score, reasons = classify(job, profile)
        if status == "review":
            ranked.append((score, job, reasons))
    ranked.sort(key=lambda item: (-item[0], item[1].usd_month_min is None, item[1].company.casefold(), item[1].title.casefold()))
    return ranked


def run(profile: Profile, out_dir: Path) -> tuple[int, int]:
    jobs, errors = fetch(profile)
    if not jobs and errors:
        raise RuntimeError("All feeds failed; preserving the previous shortlist: " + "; ".join(errors))
    ranked = rank(jobs, profile)
    top, overflow = ranked[:profile.max_results], ranked[profile.max_results:]
    db_path = out_dir / "radar.db"
    with closing(store.connect(db_path)) as db:
        seen = store.seen_urls(db)
    new = {job.url for _, job, _ in ranked if job.url not in seen}
    now = datetime.now(timezone.utc)
    checked = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Career radar — {profile.name} — {checked}", "",
             f"Company boards: {len(profile.sources)}; search queries: {len(profile.search_queries)}; unique jobs: {len(jobs)}; matching review queue: {len(ranked)}; new since last run: {len(new)}.",
             "", f"Scores reflect keyword fit only. Verify {profile.country_label} eligibility, compensation, time zone, and job status on the employer page before applying. Himalayas and HiGlobe listings are discovery links; open the employer posting before making a decision.", ""]
    for score, job, reasons in top:
        location = job.location if len(job.location) < 140 else job.location[:137] + "..."
        label = "Employer posting" if job.source not in AGGREGATORS else "Discovery listing (verify employer link)"
        marker = " — NEW" if job.url in new else ""
        lines += [f"## {job.company} — {job.title} (score {score}){marker}", "", f"- {label}: {job.url}",
                  f"- Location: {location or 'unverified'}", f"- Compensation: {job.salary or 'unverified'}",
                  f"- Published/updated: {job.published or 'unverified'}", f"- Source: {job.source}",
                  f"- Evidence and checks: {', '.join(reasons)}", ""]
    if overflow:
        lines += [f"## Also matched ({len(overflow)} below the cut)", ""]
        lines += [f"- {job.company} — {job.title} (score {score}){' — NEW' if job.url in new else ''}: {job.url}" for score, job, _ in overflow] + [""]
    if errors:
        lines += ["## Feed errors", ""] + [f"- {error}" for error in errors] + [""]
    report = "\n".join(lines)
    write_atomic(out_dir / "shortlist.md", report)
    write_atomic(out_dir / "history" / f"{now:%Y-%m-%d}.md", report)
    # Recorded after the report is written, so a failed write never hides a job as already seen.
    stamp = now.isoformat(timespec="seconds")
    with closing(store.connect(db_path)) as db, db:
        store.record_seen(db, [job for _, job, _ in ranked], stamp)
        store.upsert_jobs(db, ranked, stamp)
    return len(jobs), len(ranked)

def selected_profiles(profiles_dir: Path, name: str | None) -> list[Path]:
    return [profiles_dir / name] if name else [profiles_dir / found for found in list_profiles(profiles_dir)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--profile", help="run one profile by name, active or not")
    who.add_argument("--all-active", action="store_true", help="run every profile with \"active\": true")
    parser.add_argument("--profiles-dir", type=Path, default=HERE / "profiles")
    parser.add_argument("--out-dir", type=Path, default=HERE / "out")
    args = parser.parse_args(argv)
    attempted = failures = 0
    for root in selected_profiles(args.profiles_dir, args.profile):
        try:
            profile = load_profile(root)
        except ProfileError as exc:
            attempted += 1
            failures += 1
            print(f"{root.name}: invalid profile: {exc}", file=sys.stderr)
            continue
        if args.all_active and not profile.active:
            continue
        attempted += 1
        try:
            total, matched = run(profile, args.out_dir / profile.name)
        except RuntimeError as exc:
            failures += 1
            print(f"{profile.name}: {exc}", file=sys.stderr)
            continue
        print(f"{profile.name}: checked {total} jobs, queued {matched} for review: {args.out_dir / profile.name / 'shortlist.md'}")
    if not attempted:
        print(f"No profiles to run in {args.profiles_dir}", file=sys.stderr)
    # One broken profile must not stop the others, or the enrichment chained after this run.
    return 1 if failures and failures == attempted else 0


if __name__ == "__main__":
    sys.exit(main())
