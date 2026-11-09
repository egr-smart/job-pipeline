# job-pipeline — Design

An automated job-search pipeline for software engineers. It pulls listings from job boards and company ATS feeds, drops anything the user isn't location-eligible for, scores the rest against the user's profile with a pluggable LLM, and emails a periodic shortlist.

Built primarily for one user (senior backend-leaning engineer in Australia looking for remote infrastructure/platform roles), but fully config-driven so others can run their own instance. One instance = one user.

## Goals / non-goals

**Goals**
- Find remote roles the user is actually eligible for, based on their home country vs. the role's allowed region.
- Rank by fit using an LLM, with short, explainable reasoning and explicit gaps.
- Never show the same job twice; keep LLM spend bounded and visible.
- Run unattended on a schedule (NixOS home server via systemd timers; cron elsewhere).
- Swap LLM backends via config (hosted API, OpenRouter, local Ollama, subscription CLIs).

**Non-goals (v1)**
- No country-specific travel/work-abroad logic. Questions like "can I work from country X for N months" are answered manually after shortlisting.
- No salary filtering or currency conversion. Salary is display-only.
- No PDF CV parsing. The CV is pasted as text into `profile.md`.
- No scraping of login-walled boards (Wellfound, YC Work at a Startup).
- No web UI, no multi-tenant support.

## Pipeline overview

```
 sources ──► normalise ──► dedupe ──► hard filters ──► LLM score ──► store
 (fetch)     (RawJob)      (sqlite)   (title, location,  (ScoreResult)
                                       age)
                                                                       │
                                     digest: rank ──► report.md ──► notifiers (file, email)
```

Two independently scheduled commands:

- `jobs collect`: fetch, normalise, dedupe, filter, score, store. Idempotent; safe to run often.
- `jobs digest`: build a shortlist from scored jobs that are **unshown and still active**, render the report, notify, and record appearances.
- `jobs run` = `collect` then `digest`, for single-cron setups.

Because the digest is defined as "unshown and still active", any combination of collect and digest intervals works (e.g. collect daily, digest weekly).

## Configuration

Rule: **if code filters on it, it goes in YAML; if only the LLM reads it, it goes in markdown.**

### `config.yaml` (validated with pydantic; clear errors on bad input)

```yaml
home_country: AU

location:
  # Region names that include home_country. Merged with built-in data/regions.yaml.
  extra_allowed_regions: []
  # Timezone-overlap requirements are never a deny reason.

titles:
  include: [platform, infrastructure, infra, sre, reliability, devops, backend, cloud, distributed]
  exclude: [sales, marketing, designer, recruiter, intern, account executive]

limits:
  max_age_days: 30
  max_jobs_per_run: 150        # hard cap on scoring calls; newest first, remainder deferred
  active_window_days: 30       # job considered active if seen in a source within this window

sources:
  watchlist: watchlist.yaml
  hn_who_is_hiring: { enabled: true }
  remotive: { enabled: true, categories: [devops, software-dev] }
  weworkremotely: { enabled: true, categories: [devops-sysadmin, back-end-programming] }

llm:
  scoring:
    backend: openai_compatible
    base_url: https://openrouter.ai/api/v1
    model: <model-id>
    api_key_env: OPENROUTER_API_KEY
    batch_size: 1
  extraction:                  # optional; defaults to scoring config
    model: <cheaper-model-id>

ranking:
  shortlist_size: 10
  unclear_list_size: 3
  weights: { role_fit: 0.5, stack_fit: 0.3, seniority: 0.2 }
  red_flag_penalty: 1.0
  resurface: false

notifiers:
  - type: file
    dir: reports/
  - type: email
    smtp_host: smtp.gmail.com
    smtp_port: 587
    username_env: SMTP_USER
    password_env: SMTP_PASSWORD
    to: you@example.com
```

Secrets are always passed by env var name, never inline.

### `profile.md` (free-form, sent verbatim to the LLM, prompt-cached where supported)

Suggested sections: summary/seniority, stack with depth and years, domains, CV text, what I'm looking for, dealbreakers, nice-to-haves. Pay preferences can go here as soft guidance.

### `watchlist.yaml`

```yaml
companies:
  - name: Octopus Deploy
  - name: Grafana Labs
    ats: greenhouse          # optional; filled in by `jobs watchlist resolve`
    slug: grafanalabs
```

## Sources (v1)

Every source implements `fetch() -> list[RawJob]`. A failing source never fails the run; it's reported in run health.

| Source | Access | Notes |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | Watchlist |
| Lever | `api.lever.co/v0/postings/{slug}` | Watchlist |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` | Watchlist; often structured salary |
| HN Who is Hiring | Algolia HN API | Current month's thread (plus previous month on first run). Free text: each comment goes through LLM **extraction** to produce 0..N `RawJob`s. Cached by comment ID so extraction runs once. |
| Remotive | Public JSON API | Category filtered |
| We Work Remotely | Category RSS | |

**v1.1:** RemoteOK, Working Nomads. **Skipped:** Wellfound, YC Work at a Startup.

`jobs watchlist resolve` probes the three ATS endpoints with slug guesses derived from the company name, writes back the resolved `ats`/`slug`, and lists unresolved companies (e.g. ones with custom careers sites).

### Seed watchlist (ATS/slugs and AU eligibility unverified; run `resolve`)

Octopus Deploy, Buildkite, Atlassian, GitLab, Canonical, Grafana Labs, Elastic, Tailscale, Teleport, Temporal, Cockroach Labs, ClickHouse, Redpanda, Timescale/Tiger Data, Supabase, PostHog, Neon, PlanetScale, Railway, Fly.io, Pulumi, Chainguard, Coder, Sourcegraph, Mattermost, Wikimedia Foundation, Mozilla, DuckDuckGo.

### `RawJob`

```
source, source_id, url, company, title, location_raw, description_text,
posted_at (best effort; ATS updated_at accepted), salary_raw (if structured), fetched_at
```

Descriptions are converted from HTML to text before storage.

## Hard filters (code, before any LLM call)

Applied in order: **age → title → location**.

1. **Age:** drop if `posted_at` is older than `max_age_days`. Aggregator sources only: a company ATS board lists only open roles, and its posting dates are often months old for roles still hiring.
2. **Title:** must match an `include` keyword and no `exclude` keyword (case-insensitive, word-boundary).
3. **Location** (three-way):
   - **allow:** remote, and the location names a region containing `home_country` (data/regions.yaml maps region aliases, e.g. APAC, Asia-Pacific, ANZ, Oceania, Australia, Worldwide, Anywhere, to countries), or contractor/EOR language with no region restriction.
   - **deny:** on-site, hybrid, an explicit region that excludes `home_country` (US-only, EMEA, "must reside in…"), or a citizenship or security-clearance requirement.
   - Places are matched in the location string; if a remote role's location names none, the title is checked too ("SRE, Infrastructure — UK").
   - A location naming the home country but not saying "remote" (some boards list bare countries) is **unknown**, so the LLM judges remoteness.
   - **unknown:** "Remote" with no region. A keyword pass on the description looks for home-country-excluding tells (for a non-US user: 401(k), "authorized to work in the US", lists of US states, US-only benefits). A hit means **deny**. Otherwise the job passes to the LLM, which judges eligibility.
   - **Timezone-overlap requirements are never a deny reason.**

All phrase lists live in `data/*.yaml`, not code, so they're tunable without code changes.

## LLM layer

### Backends

`Scorer` protocol: `score(job, profile) -> ScoreResult` and `extract(comment) -> list[RawJob]`.

| Backend | Status | Covers |
|---|---|---|
| `openai_compatible` | **v1 (primary)** | OpenRouter (author's setup), OpenAI, Ollama, LM Studio, vLLM, llama.cpp server |
| `anthropic` | later | Native SDK with prompt caching |
| `cli` | later | Subscription CLIs via a command template (e.g. `claude -p --output-format json`), prompt on stdin. Best for Max-tier subscriptions; Pro limits make it a poor fit for bulk scoring. |

- One job per call by default; `batch_size` is configurable per backend.
- Output is validated against a pydantic schema; one retry on malformed JSON.
- Per-call token usage and cost (when the provider returns it) are logged and summed in run health.

### `ScoreResult`

Field order is deliberate: reasoning comes before the numbers.

```
summary:            1–2 lines on why it fits / doesn't
gaps:               ["3+ yrs production Go", ...]
red_flags:          [...]                     # matched against profile dealbreakers
location_eligible:  yes | no | unclear
location_note:      short justification
salary_text:        verbatim if stated in description, else null
role_fit:           0–10
stack_fit:          0–10
seniority:          under | match | over
```

Prompts live in `llm/prompts/{extract,score}.md`.

## Ranking (code, not LLM)

1. Drop `location_eligible == no` and `seniority == under`.
2. `score = w.role_fit*role_fit + w.stack_fit*stack_fit + w.seniority*seniority_bonus − red_flag_penalty*len(red_flags)`.
   `seniority_bonus`: match = 10, over = 6 (tunable).
3. `location_eligible == yes` → **Top matches** (top `shortlist_size`).
   `location_eligible == unclear` → **Worth asking** (top `unclear_list_size`).

## State (SQLite, single file)

- **`jobs`**: unique `(source, source_id)`; `fingerprint = normalize(company) + normalize(title)` (lowercase, strip seniority words like "Sr./Senior", "(Remote)" and punctuation). On a fingerprint match, merge: keep the richest description and record every URL, preferring the company ATS link. Tracks `first_seen`, `last_seen`, `filter_result`, `filter_reason`.
- **`hn_comments`**: comment ID → extraction done (prevents re-extraction).
- **`scores`**: keyed by `(job_id, profile_hash, scorer_id)`. A job is scored once per profile+model combination. If `profile.md` or the model changes, the next collect rescores **active, unshown, undismissed** jobs only. Historical/expired jobs are never rescored.
- **`shortlist_appearances`**: a job appears in a digest at most once (unless `resurface: true`).
- **`marks`**: `jobs mark <id> applied|dismissed [--reason ...]` excludes the job from future digests.
- **`runs`**: per-run stats for the health section (per-source status, counts, LLM calls, cost).
- Schema versioned via a small `migrations` table.

## Digest / email

Rendered once as markdown (saved to `reports/YYYY-MM-DD.md`), then sent as multipart email (plain text + HTML via `markdown`). An email is always sent, even with 0 matches, so a silent pipeline is noticeable.

```
Subject: Job digest — 7 matches, 3 worth asking (week of 9 Nov)

## Top matches
1. [a3f9] Senior Platform Engineer — Grafana Labs            [score 8.4]
   Location: "Remote — APAC"  ✓
   Salary: "AUD 180k–220k"                     (only if stated)
   Links: Greenhouse · also on Remotive
   Why: …
   Gaps: …
   Flags: …
   role 9 · stack 7 · seniority match · posted 4d ago

## Worth asking (location unclear)
…

## Run health
Sources: per-source ok/failed (with failing board names)
Pipeline: fetched → new → passed filters → scored (→ deferred, if cap hit)
LLM: calls (extraction / scoring) · cost · model
```

- The raw location string is always quoted verbatim.
- Short job IDs match `jobs mark`.

## Evaluation

`evals/` holds 20–30 real job descriptions with hand labels (`strong | maybe | no`, expected `location_eligible`), built from the first few real runs. `jobs eval [--model X]` scores them and prints agreement. Use it to check prompt, weight and model changes, and to pick the cheapest OpenRouter model that agrees with the labels.

## CLI

```
jobs collect [--dry-run]       # dry-run: fetch + filter only, report counts per source, no LLM
jobs digest [--no-send]
jobs run
jobs eval [--model X]
jobs mark <id> applied|dismissed [--reason TEXT]
jobs watchlist resolve
```

Run `jobs collect --dry-run` once by hand before enabling timers, to check the first-run volume.

## Deployment

### NixOS (primary)

`flake.nix` exposes the package (`buildPythonApplication` with nixpkgs Python deps), a dev shell, and a module:

```nix
services.job-pipeline = {
  enable = true;
  configFile = ./job-pipeline/config.yaml;
  profileFile = ./job-pipeline/profile.md;
  watchlistFile = ./job-pipeline/watchlist.yaml;
  environmentFile = config.sops.secrets.job-pipeline-env.path;  # API keys, SMTP creds
  collect.schedule = "daily";        # systemd OnCalendar
  digest.schedule = "Mon 08:00";
};
```

Two systemd timers with `Persistent=true` (catch up after downtime), a `DynamicUser` service, and state in `StateDirectory` (`/var/lib/job-pipeline`).

### Elsewhere

`uv run jobs collect` / `jobs digest` from cron (two lines), or `jobs run` from one.

## Tech stack

Python 3.12 · uv · httpx (sync, per-host rate limiting) · pydantic v2 · typer · sqlite3 (stdlib) · feedparser · selectolax/beautifulsoup4 · openai SDK · smtplib (stdlib) + markdown · pytest.

## Layout

```
job_pipeline/
  sources/       greenhouse.py lever.py ashby.py hn.py remotive.py wwr.py
  filter/        age.py title.py location.py
  data/          regions.yaml deny_phrases.yaml us_only_tells.yaml
  llm/           base.py openai_compat.py prompts/{extract,score}.md
  rank.py
  store.py       (+ migrations)
  digest.py
  notifiers/     file.py email.py
  config.py
  cli.py
tests/           fixtures/<source>/*.json|xml  (recorded responses; no live network)
evals/
config.example.yaml  profile.example.md  watchlist.example.yaml
flake.nix  nix/module.nix
```

## Testing

- Source adapters are tested against recorded fixtures.
- The location filter has table-driven tests covering allow/deny/unknown cases and region aliases.
- Ranking and dedupe/fingerprint logic get unit tests.
- The LLM layer is tested with a fake backend returning canned `ScoreResult`s, plus malformed-JSON retry tests.

## Future

- `anthropic` and `cli` backends.
- RemoteOK, Working Nomads; custom adapters for non-ATS careers sites (e.g. Atlassian).
- Instant alert when a job scores above a threshold during `collect`.
- Feed `mark` reasons back into the eval set automatically.
- More notifiers (ntfy, Slack/Discord webhooks).
