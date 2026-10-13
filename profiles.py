"""Per-person profiles: validate profile.json and compile its term lists into the radar's regexes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
NEVER = re.compile(r"(?!)")
TERM_LISTS = ("title_include", "title_exclude", "specialty_exclude", "core_terms", "adjacent_terms",
              "bonus_terms", "eligible_region_terms")
COUNTRY_NAMES = {"AR": "Argentina", "BR": "Brazil", "CL": "Chile", "CO": "Colombia", "MX": "Mexico",
                 "PT": "Portugal", "US": "United States"}


class ProfileError(ValueError):
    """A profile.json that cannot drive a run; the message names the field."""


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path
    active: bool
    country: str
    remote_only: bool
    min_usd_month: float
    max_results: int
    search_queries: tuple[str, ...]
    sources: tuple[dict, ...]
    applied_urls: frozenset[str]
    applied_roles: tuple[tuple[str, str], ...]
    fit_profile: str
    title_include: re.Pattern[str]
    title_exclude: re.Pattern[str]
    specialty_exclude: re.Pattern[str]
    core_terms: re.Pattern[str]
    adjacent_terms: re.Pattern[str]
    bonus_terms: re.Pattern[str]
    eligible_region: re.Pattern[str]

    @property
    def country_label(self) -> str:
        return COUNTRY_NAMES.get(self.country, self.country)


def compile_terms(terms: list[str]) -> re.Pattern[str]:
    """Join terms into one case-insensitive whole-term regex (no word character on either side); spaces or hyphens inside a term match one optional space or hyphen."""
    alternatives = ["[\\s-]?".join(map(re.escape, re.split(r"[\s-]+", term.strip()))) for term in terms if term.strip()]
    return re.compile(r"(?<!\w)(" + "|".join(alternatives) + r")(?!\w)", re.I) if alternatives else NEVER


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_records(value: object, keys: tuple[str, ...]) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict) and all(isinstance(item.get(key), str) for key in keys) for item in value)


def validate(raw: object, name: str) -> None:
    if not isinstance(raw, dict):
        raise ProfileError("profile.json must be a JSON object")
    checks = [
        ("name", lambda v: v == name, f"must equal the directory name '{name}'"),
        ("active", lambda v: isinstance(v, bool), "must be true or false"),
        ("country", lambda v: isinstance(v, str) and bool(re.fullmatch(r"[A-Z]{2}", v)), "must be a two-letter ISO code such as BR"),
        ("remote_only", lambda v: isinstance(v, bool), "must be true or false"),
        ("min_usd_month", lambda v: _is_number(v) and v >= 0, "must be a number of USD per month"),
        ("max_results", lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "must be a positive integer"),
        ("search_queries", _is_strings, "must be a list of strings"),
        ("applied_urls", _is_strings, "must be a list of strings"),
        *[(key, _is_strings, "must be a list of strings") for key in TERM_LISTS],
        ("title_include", lambda v: any(term.strip() for term in v), "must list at least one term"),
        ("sources", lambda v: _is_records(v, ("type", "board", "company")), "must be a list of {type, board, company}"),
        ("applied_roles", lambda v: _is_records(v, ("company", "title_contains")), "must be a list of {company, title_contains}"),
        ("fit_profile", lambda v: isinstance(v, str), "must be a string"),
    ]
    for key, check, message in checks:
        if key not in raw:
            raise ProfileError(f"missing required field '{key}'")
        if not check(raw[key]):
            raise ProfileError(f"field '{key}' {message}")


def compile_profile(raw: dict, root: Path) -> Profile:
    validate(raw, root.name)
    return Profile(
        name=raw["name"], root=root, active=raw["active"], country=raw["country"], remote_only=raw["remote_only"],
        min_usd_month=float(raw["min_usd_month"]), max_results=raw["max_results"],
        search_queries=tuple(raw["search_queries"]), sources=tuple(raw["sources"]),
        applied_urls=frozenset(normalize_url(url) for url in raw["applied_urls"]),
        applied_roles=tuple((role["company"].casefold(), role["title_contains"].casefold()) for role in raw["applied_roles"]),
        fit_profile=raw["fit_profile"],
        title_include=compile_terms(raw["title_include"]), title_exclude=compile_terms(raw["title_exclude"]),
        specialty_exclude=compile_terms(raw["specialty_exclude"]), core_terms=compile_terms(raw["core_terms"]),
        adjacent_terms=compile_terms(raw["adjacent_terms"]), bonus_terms=compile_terms(raw["bonus_terms"]),
        eligible_region=compile_terms(raw["eligible_region_terms"]))


def load_profile(root: Path) -> Profile:
    if not NAME.fullmatch(root.name):
        raise ProfileError(f"profile directory '{root.name}' must be lowercase letters, digits, '-' or '_'")
    path = root / "profile.json"
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ProfileError(f"{path} does not exist") from None
    except ValueError as exc:
        raise ProfileError(f"{path} is not valid JSON: {exc}") from None
    return compile_profile(raw, root)


def list_profiles(profiles_dir: Path) -> list[str]:
    if not profiles_dir.is_dir():
        return []
    return sorted(path.name for path in profiles_dir.iterdir()
                  if NAME.fullmatch(path.name) and (path / "profile.json").is_file())


def normalize_url(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def is_applied(profile: Profile, url: str, company: str, title: str) -> bool:
    if normalize_url(url) in profile.applied_urls:
        return True
    return any(role_company in company.casefold() and role_title in title.casefold()
               for role_company, role_title in profile.applied_roles)
