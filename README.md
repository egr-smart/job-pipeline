# job-pipeline

Automated remote job search for software engineers. It fetches listings from company ATS boards and remote job boards, drops roles you aren't location-eligible for, scores the rest against your profile with a pluggable LLM, and emails you a shortlist.

See [DESIGN.md](DESIGN.md) for how it works.

## Quick start

```sh
cp config.example.yaml config.yaml        # set home_country, titles, LLM, email
cp watchlist.example.yaml watchlist.yaml  # companies to poll directly
uv sync
uv run jobs watchlist resolve             # detect each company's ATS + slug
uv run jobs collect --dry-run             # fetch + filter, no LLM; check volumes
uv run jobs jobs --result unknown         # inspect what the filter let through, and why
uv run jobs collect --limit 10            # score the 10 newest passing jobs with the LLM
uv run jobs scores --dropped              # ranked results with reasoning, in the terminal
uv run jobs digest --preview --open       # readable HTML report (no email, jobs stay unshown)
uv run jobs digest                        # real digest: report + email, records what was shown
uv run jobs mark 1998 applied             # never show a job again (applied | dismissed)
```

## Status

- [x] Config, SQLite store, cross-source dedupe
- [x] Greenhouse / Lever / Ashby watchlist + `watchlist resolve`
- [x] Hard filters (age, title, three-way location)
- [x] HN Who is Hiring, Remotive, We Work Remotely
- [x] LLM scoring via Claude Code CLI (`backend: cli`) + `jobs scores`
- [ ] OpenAI-compatible backend (OpenRouter, Ollama, …)
- [x] Ranking
- [x] Digest (markdown + HTML report), email notifier, `jobs mark`
- [ ] `jobs eval`
- [ ] Nix flake + NixOS module
