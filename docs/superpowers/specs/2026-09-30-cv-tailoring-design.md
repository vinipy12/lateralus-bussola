# CV tailoring with phone review

Date: 2026-09-30. Status: approved in brainstorming, awaiting spec review.

## Goal

For every job the user swipes right on ("apply"), produce a CV tailored to that job that the user, or someone helping them, can review on a phone and approve, reject or regenerate. The CV only rearranges and rewords what the original CV already says. It never adds a fact.

This is sub-project A of three. The full product flow is: (1) import CV or LinkedIn PDF, (2) "about me" bullets, (3) pre-filter questions, (4) job finding, (5) swipe matching, (6) CV tailoring, (7) assisted apply. Steps 1-5 already exist as CLI and files (`ready.py`, `radar.py`, the swipe deck). This spec is step 6. Out of scope, each a separate spec:

- B. Web onboarding for non-technical users (steps 1-3 as pages) and multi-user SaaS foundations.
- C. Assisted apply (step 7). It reads the approved CVs this spec produces.
- Suggested additions: asking the user about skills the ad wants and the CV lacks (see "Extensibility").
- Editing text on the phone, Portuguese/English variants, DOCX output, a LinkedIn-profile rewrite.

## Decisions

| Question | Decision |
|---|---|
| Output of the prototype | Phone-viewable HTML plus an ATS-safe PDF, not a CLI report |
| How much freedom the model has | Select, reorder and rephrase only. No new facts |
| What the reviewer can do | Approve, reject, or regenerate. No text editing |
| Which jobs get tailored | Only jobs swiped "apply" |
| How regenerate reaches Codex | Queue in SQLite, processed by a host worker on a one-minute systemd timer. The container never calls Codex |
| PDF renderer | Headless `google-chrome --print-to-pdf` on the host. No new Python dependency |
| Grounding source in v1 | `profiles/<name>/cv.txt`. The step 2 "about me" bullets arrive with sub-project B as an extra source file (the gate takes a list of source texts) |

## Architecture

```
phone ──HTTP──> swipe container (stdlib, no Codex login)
                  GET  /api/resumes            list per profile, with status
                  GET  /resume/<id>            tailored CV as HTML (phone view)
                  GET  /resume/<id>.pdf        the rendered PDF
                  POST /api/resume/decision    approve | reject
                  POST /api/resume/regenerate  sets status = queued

host, systemd timer every minute (and after enrich.py)
  tailor.py --all-active
      for each profile: resumes to build = swiped "apply" with no resume row,
                        plus rows with status = queued
      codex exec (isolated CODEX_HOME) -> ground_cv() -> render HTML -> Chrome PDF
      writes out/<name>/resumes/<id>.{json,html,pdf}; updates the resumes row
```

The deck and the worker share the per-profile SQLite file, as `enrich.py` and the deck already do. One new table:

```sql
CREATE TABLE IF NOT EXISTS resumes (
    url TEXT PRIMARY KEY REFERENCES jobs(url),
    status TEXT NOT NULL CHECK (status IN ('queued','working','ready','approved','rejected','failed')),
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
    dropped_lines INTEGER NOT NULL DEFAULT 0, rephrased_lines INTEGER NOT NULL DEFAULT 0,
    requested_at TEXT NOT NULL, updated_at TEXT NOT NULL);
```

Files live under `out/<name>/resumes/<id>.*`, where `<id>` is a short hash of the job URL. `store.py` owns the SQL, matching how `swipes` and `cards` are handled.

### Modules

| Module | Does | Depends on |
|---|---|---|
| `tailor.py` | Worker CLI: picks work, calls Codex, grounds, renders, writes status | `store`, `codex_client`, `tailor_ground`, `tailor_render`, `profiles` |
| `tailor_ground.py` | Pure functions: `ground_cv(raw, sources) -> (cv, report)`. No I/O, no LLM | `enrich.normalize` |
| `tailor_render.py` | `render_html(cv)`, `render_pdf(html_path, pdf_path)` via Chrome | `html.escape`, subprocess |
| `tailored_cv.schema.json` | Output schema for `codex exec --output-schema` | none |
| `swipe.py` / `swipe.html` | New routes and a "Résumés" screen | `store` |

`tailor_ground.py` is the only place that decides what survives, and it is testable without Codex or Chrome.

## The tailoring call

One `run_codex` call per job, with the job ad as untrusted input and the CV as trusted data, in the same shape as the card prompt. The model returns:

- `header`: name and contact lines.
- `summary`: optional, one to two sentences.
- `experience[]`: `company`, `title`, `dates`, and `bullets[]`, each `{text, source_quotes[]}`.
- `skills[]`: `{text, source_quotes[]}`.
- `education[]`: `school`, `degree`, `dates`.
- `gaps[]`: `{ad_term, reason}` for terms the ad wants that the CV does not support. Stored in the JSON, not shown in v1.

The prompt requires: keep the CV's language; never use a stronger verb than the source (a source "participated in" must not become "led"); order entries and bullets by relevance to the ad; omit irrelevant items; copy every quote word for word.

## The no-fabrication gate (`ground_cv`)

Applied to the model output before anything is rendered. A failed line is dropped, never repaired.

1. Every `source_quote` must be a substring of a source text after `normalize()`. Otherwise the line is dropped.
2. Every number and every proper-noun or technology token in a bullet's `text` must also appear in that bullet's own `source_quotes`. Numbers include percentages, currency and ranges. Technology tokens include acronyms, capitalised words that are not the first word of the line, and tokens containing digits or symbols (`c++`, `.net`). The exact tokenizer is defined in the plan. It may reject valid lines (accepted) but must not accept an invented figure, skill or employer.
3. `company`, `title`, `dates`, `school`, `degree` and the header lines must each be a substring of a source text.
4. `skills` may only contain skills present in the CV text.
5. `summary` is kept only if it has `source_quotes` and passes rules 1-2. Otherwise it is dropped.
6. If an entry loses all bullets it is dropped. If nothing is left, the job fails with a clear error and no CV is shown.

Each surviving bullet is tagged `verbatim` (text equals its quote after normalisation) or `rephrased`. The report counts dropped and rephrased lines.

Known limit: substring checks cannot prove a rephrase is faithful. "Participated in" becoming "led" passes every rule above. It is mitigated by the prompt rule, by showing the original quote next to every rephrased line on the phone, and by the reviewer. A second LLM verifier pass is a later option if the pilot shows inflation.

## Phone review screen

A "Résumés" entry in the deck menu lists jobs swiped apply, grouped by status (ready to review, approved, working, failed). Opening one shows:

- The tailored CV in a readable single column.
- Rephrased lines marked; tapping one reveals the original sentence from the CV.
- A count of lines the system dropped.
- Buttons: Approve, Reject, Regenerate, and Download PDF.

Regenerate sets `queued` and the page shows "working" and polls until `ready`. The previous version stays available until the new one replaces it. A failed job shows the reason in plain language and a Regenerate button. After three failed attempts it stays failed until the user taps Regenerate, which resets the attempt counter.

## Rendering

HTML is the source of truth. It is a single column with semantic `h1`, `h2` and `ul`, system fonts, A4 page size, and no tables, images or text in columns, so ATS parsers read it in order. All text is HTML-escaped. Chrome prints it to PDF headlessly. The PDF is served only with the deck key, like every other route.

## Error handling

- Codex missing or logged out: the worker skips the run and leaves rows queued, like `enrich.py`.
- Chrome missing or failing: status `failed` with "PDF could not be rendered"; the HTML view still works.
- Two worker runs overlapping: a per-profile lock; the second exits.
- A job ad that tries to instruct the model: output only passes through the gate, and every user-visible string is escaped.

## Extensibility (option 2: suggested additions)

`gaps[]` is already produced and stored. A later layer can turn each gap into a question to the user. A confirmed answer becomes an extra line in an "about me" source file that is added to the `sources` list passed to `ground_cv`. No schema or gate change is needed.

## Testing

- `tests/test_tailor_ground.py`: hand-built cases for an invented number, an invented skill taken from the ad, a swapped employer, an altered date, a quote that is not in the CV, a stronger-verb rephrase that is accepted (documented limit), a valid rephrase, and an entry that loses all bullets.
- `tests/test_tailor.py`: pipeline with a fake runner, in the style of `test_enrich.py`. It covers queue pickup, regenerate, three-strike failure and lock behaviour.
- `tests/test_tailor_render.py`: HTML escaping, and a PDF round trip through `pdftotext` that checks every bullet is present and in order. Skipped when Chrome is missing.
- `tests/test_swipe.py` additions: the new routes, key enforcement, and decision validation.
- `tests/test_live_codex.py` gets one tailoring call as an opt-in tripwire.

## Open risks to check in the plan

- Headless Chrome under a systemd user service (no display, sandbox flags).
- Token cost per tailoring call is not measured. It is one CV plus one ad per job, so it should be smaller than the weekly enrichment but needs a measured number before this is priced for others.
- The CV text is sent to OpenAI through Codex, as `ready.py` already states. Users must be told this before sub-project B.
