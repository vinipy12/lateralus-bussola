# Bussola

A weekly remote-job radar.

Runs a read-only weekly search across public [Greenhouse](https://docs.greenhouse.io/job-board.html), [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api), and [Lever](https://github.com/lever/postings-api) company boards, plus [Himalayas' public search API](https://himalayas.app/api) and the [HiGlobe jobs board](https://jobs.higlobe.com/) RSS feed. Produces a Markdown shortlist for **manual review**, then turns each new match into an AI job card you triage by swiping on your phone. It does not apply to jobs, edit your résumé, email anyone, or contact recruiters.

## Profiles

Each person is a directory `profiles/<name>/` with a `profile.json`. The name is lowercase letters, digits, `-` or `_`, and must equal `"name"` in the file. `profiles/`, `out/` and `.env` are gitignored; only `profiles/example/` is committed, as a template.

- Identity and filters: `name`, `active` (only active profiles run under `--all-active`), `country` (two-letter code such as `BR`), `remote_only`, `min_usd_month`, `max_results`, `fit_profile` (a short description of the candidate, given to Codex).
- Search: `search_queries` for Himalayas, and `sources`, a list of `{type, board, company}` for Greenhouse, Ashby and Lever boards. Board identifiers come from the final path component of the hosted board URL. Add `{"type": "higlobe", "board": "jobs", "company": "HiGlobe"}` to read the HiGlobe board, whose subdomain is the board; each listing keeps its own employer name.
- Term lists: `title_include` (at least one), `title_exclude`, `specialty_exclude`, `eligible_region_terms`, `core_terms`, `adjacent_terms`, `bonus_terms`. Each list is compiled into one case-insensitive whole-term regex (so `c++`, `c#` and `.net` work); a space or hyphen inside a term matches one optional space or hyphen (`full stack` matches `full-stack` and `fullstack`). An empty list matches nothing.
- Already applied: add submitted jobs to `applied_urls` or `applied_roles` (`{company, title_contains}`) so they disappear from future runs. An empty `title_contains` filters every role at that company; use the exact title when you know it.

A profile that fails validation stops with a message naming the field. `min_usd_month` is compared only against structured USD salaries.

## New profile from a CV

```sh
python3 ready.py --name <name> <cv.pdf>
```

Get the person's consent first: the CV text is sent to OpenAI through Codex. The command drafts `profiles/<name>/profile.json` from the CV (`--force` replaces an existing profile). The new profile is inactive until you review it and set `"active": true`.

## Weekly run

Requires Python 3.11+ and no third-party packages. The radar itself needs no credentials; enrichment uses the Codex CLI logged in to your ChatGPT account.

```sh
python3 radar.py --all-active && python3 enrich.py --all-active
python3 radar.py --profile <name>    # one profile, active or not (enrich.py takes the same flag)
python3 -m unittest discover -s tests -v
```

Output goes to `out/<name>/`: `shortlist.md` with a dated copy in `history/`, and `radar.db` (SQLite), which holds `seen_jobs` (`first_seen`/`last_seen`) and the cards and your swipes. Jobs not seen in an earlier run are marked `NEW`; delete `radar.db` to mark everything new again (this also erases cards and swipes). Matches beyond `max_results` are listed as one-liners under "Also matched".

Keep your own profiles and results private: `profiles/` and `out/` are gitignored, so they stay on your machine.

The report distinguishes employer postings from discovery listings, flags missing salary or unverified eligibility for the profile's country, and records feed errors. Scores are a quick triage aid, not a hiring-fit verdict. Before tailoring a résumé, open the employer's live posting and confirm location, compensation, meeting hours, and requirements. For example, a role can be remote yet expect overlap with a time zone far from yours.

## Swipe deck

The deck serves each profile's cards from `out/`, one at a time, with a key in the URL.

```sh
cp .env.example .env     # then set SWIPE_KEY (the file has the command to generate one)
docker compose up -d
```

Open `http://<hostname>.local:8765/?k=<key>` (or the machine's LAN IP) on the phone, on the home Wi-Fi. The profile menu switches decks. Everything returns 404 without the key. The key is the only protection: anyone on the home Wi-Fi who has it can read the cards and record swipes, and the connection is plain HTTP. Do not forward the port or share the link.

## Weekly schedule

On Linux, the `systemd/` user units run the radar and then enrichment (`career-radar.service`) at 09:20 local time each Monday; add an IANA time zone to `OnCalendar` in `career-radar.timer` to pin a different one. Before installing, edit `career-radar.service`: `WorkingDirectory` defaults to `%h/bussola` (`%h` is your home directory) and `PATH` must reach your `codex` binary. `Persistent=true` catches up after the machine was off, on the next user session. Each profile's result stays in `out/<name>/`. Install with `cp systemd/career-radar.service systemd/career-radar.timer ~/.config/systemd/user/ && systemctl --user daemon-reload`. Inspect the timer with `systemctl --user list-timers career-radar.timer` and the last run with `journalctl --user -u career-radar.service -n 30`.

`.github/workflows/radar.yml` supports manual runs on GitHub against `profiles/example/` and uploads the shortlist as an artifact. It runs the radar only, not enrichment, and has no automatic schedule because the local timer is active. The workflow publishes no job applications and sends no messages.

## Current limits

- Codex usage: enrichment spends your ChatGPT plan, about 1M tokens for a week of one profile's cards (an estimate, not measured). More profiles multiply it.
- Codex runs under an isolated `CODEX_HOME` (only a symlink to your login and an empty config), so your own MCP servers, connectors, memories and `AGENTS.md` are not available to it. Its read-only sandbox covers files only; the isolated home is what keeps a hostile job ad away from tools.
- `codex exec` is not a stable API. After upgrading Codex, run `BUSSOLA_LIVE_CODEX=1 python3 -m unittest discover -s tests -p 'test_live_codex.py' -v`; it makes one real call.
- systemd user services get a minimal `PATH`. If Codex is installed through nvm or another version manager, add its `bin` directory to `PATH` in `systemd/career-radar.service`, and update it when you change Node versions. Enrichment was verified with codex-cli 0.159.0.
- If Codex fails for a job three times, enrichment stops retrying it and the deck shows a basic card built from the radar data (score and reasons, no AI summary, pros, cons or verdict). If Codex is missing or logged out, enrichment is skipped for the run and the radar output is left alone.
- USD compensation is compared only when a structured USD salary floor is available. Other currencies and unpublished salaries require review; no exchange rate is guessed.
- HiGlobe's RSS feed holds only the 50 newest listings across every employer and ignores search terms, so the weekly run sees roughly the last month, filtered by the profile's terms. Its listings carry no salary, and a location such as `Remote (India)` is flagged as eligibility unverified rather than excluded.
- Himalayas search returns a limited first page for each query. Add focused queries or company boards to expand coverage without heavy polling.
- The term score can miss relevant jobs or surface weak matches. The source link and reasons are shown so you can inspect each candidate.
- The tool does not inspect ATS application forms or generate tailored answers. Keep the human decision between shortlist and application.

## License

MIT. See [LICENSE](LICENSE).
