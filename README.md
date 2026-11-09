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
```

## Status

- [x] Config, SQLite store, cross-source dedupe
- [x] Greenhouse / Lever / Ashby watchlist + `watchlist resolve`
- [x] Hard filters (age, title, three-way location)
- [ ] HN Who is Hiring, Remotive, We Work Remotely
- [ ] LLM scoring (OpenAI-compatible: OpenRouter, Ollama, …)
- [ ] Ranking, digest, email
- [ ] `jobs eval`, `jobs mark`
- [ ] Nix flake + NixOS module
