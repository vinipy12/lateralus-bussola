# Swipe triage, profiles and AI job cards

Date: 2026-09-29. Status: approved in brainstorming, awaiting spec review.

## Goal

Turn the weekly shortlist into a phone deck of job cards that the user swipes: left to pass, right to apply. Each card shows key facts, grounded pros and cons, and compensation when available. The radar must also work for other people's CVs (friends in unrelated fields), so everything person-specific moves into a per-person profile that an AI step drafts from a CV.

Out of scope: the apply phase (CV tailoring for right-swiped jobs). It is a separate spec that reads this one's `swipes` table and plugs into the end-of-deck screen. Also out of scope: access from outside the home network, per-friend logins, and AI-based ranking. The deck keeps the regex score order.

## Decisions

| Question | Decision |
|---|---|
| Where to swipe | Phone browser, served by a Docker container on the laptop over home Wi-Fi |
| Model | Codex CLI (`codex exec --output-schema`) on the user's ChatGPT subscription. Verified 2026-09-29 with codex-cli 0.159.0: schema-valid JSON in ~12 s, ~23k tokens for a tiny prompt |
| Which jobs become cards | Only the review queue (hard filters passed, score ≥ 5) |
| Server stack | Python standard library `http.server` plus one HTML page with plain JavaScript. No third-party packages |
| Who swipes friends' decks | The user, switching profiles with a menu. No logins |
| Card layout | "Everything visible" (mockup A), plus tap-to-reveal quotes and a posting link |

## Architecture

```
laptop, systemd timer (Mon 09:20 local time)
  radar.py --all-active      fetch → filter → score per profile
      writes out/<name>/shortlist.md, history/, radar.db: seen_jobs, jobs
  enrich.py --all-active     codex exec per un-carded job → radar.db: cards

docker compose (restart: unless-stopped)
  swipe.py + swipe.html      0.0.0.0:8765, reads jobs+cards, writes swipes
      mounts ./out (rw), ./profiles (ro)

phone → http://<laptop>:8765/?k=<key>
```

Codex runs only on the host, where it is logged in. The container never holds model credentials. The host jobs and the container share each `radar.db`. WAL mode and `busy_timeout=5000` make concurrent access safe on a Linux bind mount.

The phone reaches the laptop at `<hostname>.local` if the router resolves mDNS. Otherwise it uses a DHCP reservation for the laptop's IP.

## Profiles

`profiles/<name>/profile.json`. `profiles/` and `out/` are gitignored; only `profiles/example/` is committed. Fields:

- `name`, `active` (bool; the timer runs only active profiles; friends default to `false`)
- `country` (ISO code), `eligible_region_terms` (e.g. brazil, brasil, latam, latin america, americas, worldwide, anywhere, global)
- `remote_only` (bool), `min_usd_month` (number, required, never inferred from a CV)
- `search_queries`, `sources`, `applied_urls`, `applied_roles`, `max_results` (moved unchanged from `config.json`)
- `title_include`, `title_exclude`, `specialty_exclude`: term lists replacing `TITLE`, `NEGATIVE_TITLE`, `WRONG_SPECIALTY`
- `core_terms` (+4 in title), `adjacent_terms` (+3), `bonus_terms` (+2): replacing `AI`, `BACKEND`, `PRODUCT`. A match in the description adds +1, as today
- `fit_profile`: short text about the candidate, read by the enrichment step

Next to it: `cv.txt` (extracted CV text, kept for the apply phase).

**Term compilation.** Each term is escaped. Every run of spaces or hyphens inside it becomes `[\s-]?`, and the whole list is joined into one `\b(...)\b` case-insensitive regex. So "full stack" matches "full-stack", "fullstack" and "full stack". The location exclusion regex (`EXCLUDED`: us only, hybrid, on-site…) stays in code, because it describes remote-work incompatibility, not a person.

**Migration of the existing setup.** `config.json` plus the current regexes become a local, gitignored profile. The terms are hand-written to reproduce the current regexes exactly; they are not AI-drafted. `config.json` is removed from the repo. `out/radar.db` moves to `out/<name>/radar.db`, with its `seen_jobs` kept.

**Profile validation** runs at the start of every radar run. It checks required fields, types, non-empty `title_include`, and a numeric `min_usd_month`. A failure stops that profile only, with an error naming the field.

## `ready.py`: CV to profile

`python3 ready.py --name <name> <cv.pdf>`

1. `pdftotext -layout` extracts the text. If the text is empty or under 200 characters (a scanned PDF), stop with an explanatory error.
2. `codex exec` with a profile-draft schema drafts: `search_queries`, the six term lists, `eligible_region_terms` and `fit_profile`.
3. Prompt interactively for what a CV cannot answer: `country`, `remote_only`, `min_usd_month`. There are no defaults, and all three are required.
4. Write `profile.json` (`active: false`, empty `sources` and applied lists) and `cv.txt`, then print a readable summary for review.
5. Dry run: fetch and filter with no Codex calls, then print "N jobs fetched, M would become cards".

If step 2 fails, nothing is written. The user must have consent from the CV owner before running this on someone else's CV, because the text is sent to OpenAI.

## `enrich.py`: job cards

`radar.py` now also upserts every review-queue job into `jobs`. `enrich.py` processes jobs that have no successful card and fewer than 3 failed attempts, running 3 Codex calls at a time.

Each call runs as `codex exec --ephemeral --skip-git-repo-check -s read-only --output-schema card.schema.json -o <tmp>/out.json -`, in an empty temporary working directory, with a 120 s timeout. Job ad text is untrusted input. The read-only sandbox and empty working directory mean a hostile ad can at most distort its own card.

Prompt inputs: title, company, location, feed salary, description, and the profile's `fit_profile`, `country`, `remote_only` and `min_usd_month`.

Card schema:

- `summary`: string, one sentence
- `eligible`: `yes | no | unclear`, with `eligible_quote`. This is eligibility for the profile's `country`; the card chip shows that country (e.g. "🇧🇷 unclear")
- `timezone`: string or null (stated overlap requirement)
- `compensation`: string or null, as stated in the ad
- `seniority`: string or null
- `must_haves`: up to 5 strings
- `pros`, `cons`: up to 3 each of `{text, quote}`
- `dealbreaker`: string or null, with `dealbreaker_quote`

**Grounding check** (in code, after the model returns). A quote is supported if, after collapsing whitespace and casefolding, it is a substring of the job description, title or location.
- Unsupported pros and cons are dropped, and the drop count is stored.
- An unsupported eligibility quote turns the verdict into `unclear`.
- An unsupported dealbreaker is dropped.

**Compensation line on the card**, first available of:
1. the feed's structured salary;
2. the model's `compensation`, labelled "from ad text";
3. "Not stated".

**Codex unavailable** (binary missing, or not logged in, detected once per run): skip enrichment for the run and add one error line to the shortlist. The radar output is unaffected.

## Database (`out/<name>/radar.db`)

Existing: `seen_jobs(url PK, company, title, first_seen, last_seen)`.

New:
- `jobs(url PK, company, title, location, salary, usd_month_min, source, published, description, score, reasons_json, queued_at)`
- `cards(url PK, card_json, dropped_claims, attempts, error, created_at)`. A successful card has `card_json` set and `error` null
- `swipes(url PK, decision CHECK IN ('pass','apply'), swiped_at)`

Deck query: jobs that have a successful card, or 3 failed attempts (shown as a basic card); that are not in `swipes`; and that are not matched by `applied_urls` or `applied_roles`. Ordered by score descending, then as in the shortlist.

## Swipe app (`swipe.py`, `swipe.html`)

Endpoints, all requiring the key:
- `GET /`: the page
- `GET /api/profiles`: profile names
- `GET /api/deck?profile=`: the deck, as cards joined to job fields
- `POST /api/swipe {profile, url, decision}`: upsert, so swiping twice is idempotent
- `POST /api/undo {profile}`: delete the profile's most recent swipe; a no-op if there is none
- `GET /api/applies?profile=`: right-swiped jobs

**Access.** `SWIPE_KEY` comes from `.env` (gitignored). The key is accepted once as `?k=`, then remembered in an HttpOnly cookie. A request without a valid key gets a 404.

**Card, top to bottom:**
- dealbreaker banner (red, if present);
- band with the compensation line and chips for eligibility, timezone and seniority;
- company · source;
- title;
- summary;
- pros and cons, where tapping one reveals its quote;
- must-have chips;
- "N unsupported claims dropped" when N > 0;
- "Open posting ↗".

A basic card replaces the AI fields with the regex reasons and "AI summary unavailable".

**Gestures.**
- The card follows the pointer and tilts, with a PASS or APPLY stamp fading in.
- Releasing past 30% of the viewport width commits the swipe; anything less snaps back.
- The ✕ and ✓ buttons and ↶ undo do the same as the gestures; on a laptop, the left and right arrow keys work too.
- The next card is pre-rendered underneath.

**End of deck:** "All caught up", listing this round's apply swipes with their posting links. This is the apply-phase entry point.

**Container:**
- `python:3.13-slim`, copying only `swipe.py` and `swipe.html`.
- `compose.yaml` publishes 8765 and mounts `./out:/app/out` and `./profiles:/app/profiles:ro`.
- `env_file: .env`, `restart: unless-stopped`.

## Scheduling

The systemd service runs `radar.py --all-active && enrich.py --all-active`. A single profile runs on demand with `--profile <name>`. The GitHub Actions workflow keeps running the radar only; it has no Codex and no profiles in CI. It runs the tests against `profiles/example/`.

## Testing

- **Unit:**
  - term compilation;
  - profile validation;
  - the grounding check (supported, unsupported, whitespace and case differences);
  - the compensation line priority;
  - the basic-card fallback after 3 attempts;
  - deck selection and order (excludes swiped and applied jobs).
- **Mocking:** Codex is mocked at the subprocess boundary.
- **Regression:** the committed example profile classifies a fixture set of jobs identically to the current hardcoded regexes, and all 9 existing tests pass using the profile.
- **Server:** `swipe.py` on port 0 with a temporary DB covers swipe, double swipe, undo, undo on an empty deck, and a 404 without the key.
- **Opt-in live test** (`BUSSOLA_LIVE_CODEX=1`): one fixture ad through real Codex, validated against the schema.
- **Manual phone checklist:** drag and commit, drag and snap back, tap to see a quote, undo, the end-of-deck screen, the profile switch.
- **Container smoke test:** `docker compose up`, then each endpoint with and without the key.

## Risks

- **Codex usage.** About 25–35k tokens per job, roughly 1M per active profile per week. Watch the first week against the ChatGPT plan limits.
- **`codex exec` flags and output are not a stable API.** The opt-in live test is the tripwire after Codex upgrades.
- **Friends' CVs go to OpenAI.** Get consent first.
