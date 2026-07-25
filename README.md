# Scan For Grants — crawler

Monitors Italian public portals for new grants ("bandi"), extracts them with an
LLM, and writes them to Supabase.

**This repository is public.** Everything in it is world-readable, including its
GitHub Actions logs. Read the safety rules below before adding code.

## Public-repo rules

- **No secrets in code, ever.** They live in GitHub Actions repository secrets
  and, locally, in `.env` (gitignored). `.env.example` lists every variable.
- **No sensitive data in code.** The source list lives in the database, not here.
  Notification recipients come from `app_settings` at run time.
- **Every log line goes through the scrubber.** `crawler/logging_setup.py`
  redacts secrets by value *and* by shape. GitHub masks registered secrets in
  Actions output, but only exact matches — it will not catch a key quoted back
  inside a provider's JSON error body, which is how these actually leak. Call
  `logging_setup.configure()` before anything else can log.
- What *is* public and fine: crawler logic, prompts, adapter code.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # fill in
set -a; . ./.env; set +a

python -m crawler                       # every active source
python -m crawler --source <uuid>       # one source, even if paused
python -m crawler --source <uuid> --dry-run   # extract and log, write nothing
```

`--dry-run` still calls the LLM: it skips writes, not work. Use it to see what a
source would produce before trusting it.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Every network call is stubbed, so the suite needs no secrets and is safe to run
on pull requests from forks.

## How it works

Per source: fetch the list page → discover candidate URLs → **diff against
`page_snapshots`** → extract only new or changed pages → upsert by `url_hash` →
record a `scan_run`.

The diff is the whole cost model. On a steady-state run every page is unchanged,
so the run costs **zero LLM calls** — verified against the live CCIAA feed
(10 calls on first run, 0 on the third). Two things follow, both learned the hard
way:

- The diff keys on the **snapshot**, not on "is this already a grant". Non-grant
  pages (exam notices, news) never enter `grants`, so gating on grant rows
  re-sent every one of them to DeepSeek on every run, forever.
- Non-grant pages are snapshotted too, for the same reason.

### Adapters (§6.2)

| type | Discovery | Status |
|---|---|---|
| `rss` | feed entries are the candidates — no LLM | built |
| `html` | the selector ladder (below) | built |
| `html_js` | Playwright renders, then identical to `html` | built |
| `api` | per-source mapping | not built |

Rendering is the only difference between `html` and `html_js` — a rendered DOM is
still a DOM, so everything after it is shared. Real case: Regione Sardegna's bandi
page serves 192KB of HTML that cleans to **25 characters** of text; rendered, it
yields 6,874. The grants were always there, just client-side.

Prefer `rss` whenever a portal offers one: it is cheaper and cannot break the way
a selector does. CCIAA Cagliari-Oristano publishes `/bandi/rss.xml`, which is
pure grants; Sardegna Ricerche's feed is news *with* grants mixed in, so Stage B
rejects the non-grants.

### The selector ladder (§6.4)

```
1. sources.list_selector   — the selector that last worked here
2. extraction_strategies   — selectors proven on OTHER sources,
                             same fingerprint first, then by times_worked
3. Stage A                 — the LLM infers a new selector from the HTML
4. give up                 — error + diagnostics (§6.5)
```

Verified self-healing end to end on Regione Sardegna: run 1 spent a Stage A call
and cached what it inferred; run 2 used the cache and spent nothing.

**Chaining is navigation-only.** A hop follows an `href` a selector yielded. There
is no click, no action verb, no interpreter — an action vocabulary authored by an
LLM from a third-party page is a script, and executing it would hand whoever
controls that page an input to what our browser does. Same reason we cache
selectors and not generated code.

### The validation gate (§6.4)

Non-empty is **not** success. A selector that matches the nav returns links too —
accepting it would cache a wrong selector, report `ok`, and produce plausible
garbage, which is worse than failing. When the gate rejects, the ladder falls
through; spending a DeepSeek call beats caching a wrong selector. Only
gate-accepted selectors are credited, so `times_worked` means "this found real
grants", not "this matched something".

Checks: plausible count, on-domain, sane URL shape, titles that are not chrome,
and **cohesion** — a grant list points at sibling pages, so its links share a path
prefix. Cohesion was added after Stage A proposed a selector on Regione Sardegna
that matched 18 real bandi *and* 20 department pages; every stray was on-domain
with a plausible path, so every other check passed them.

### Auto-draft on persistent error (§6.4 Track 2)

`sources.consecutive_errors` counts errored scans in a row (reset on the first
ok). When a `html`/`html_js` source hits `AUTODRAFT_AFTER_ERRORS` (default 3,
`0` disables), the run drafts an extraction rule (`crawler/draft.py`) and opens
a PR via `crawler/github_pr.py` — the same flow as the manual
`--draft-adapter`, triggered by the streak instead of a human.

It fires exactly **at** the threshold, not above it, so a week-long outage
produces one PR, not one per hourly scan; the stable per-source branch means a
re-draft updates the same PR anyway. The safety story is unchanged: the draft
is inert data validated against `rules.py`, the PR is **never** auto-merged,
and a human applies the merged rule to `sources.extraction_rules`. Needs the
`GH_PR_TOKEN` secret (fine-grained PAT, Contents + Pull requests on this repo
only) — the workflow's own token stays `contents: read`. Without it the streak
is still tracked and drafting just logs.

### Diagnostics (§6.5)

A zero-candidate run records to `scan_runs.diagnostics`: fetched bytes, cleaned
text length, whether truncation hit, a head of the cleaned text, and which rungs
ran. "The LLM found nothing" is five different failures — JS-rendered page, wrong
page, cleaner ate it, model missed it, content behind interaction — and only one
is an LLM problem. Without this they are indistinguishable.

## Scheduling (§6.7)

The workflow fires hourly and the job asks `app_settings.scan_frequency` whether
to proceed (`--respect-frequency`), so changing the frequency in Settings takes
effect immediately — no workflow edit, no redeploy. `daily` means 06:00
**Europe/Rome**, not UTC: the team is Italian. Gating keys on the hour, never the
minute, because GitHub cron drifts by minutes under load.

## Real-world notes

DeepSeek rejects `response_format=json_object` unless the prompt contains the word
"json" — a 400, not a soft failure. `LLMClient` injects it when a prompt omits it,
so a new prompt cannot silently break. Stage A hit this; Stage B never did,
because its prompt happened to say "JSON".

Feeds are malformed. Sardegna Ricerche publishes every link as
`http://http://www.sardegnaricerche.it/...` — the scheme twice, which makes
`http` the hostname and fails every fetch. `fetch.repair_url` fixes what it
safely can.
