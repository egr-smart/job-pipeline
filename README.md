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

### Checking the scorer: `jobs eval`

Label 20–30 real jobs, then compare any model's judgement with yours:

```sh
uv run jobs eval add 1998 strong --location yes --note "evals/feedback-loop work"
uv run jobs eval add 3667 no --location no --note "office 3 days/week"
uv run jobs eval list
uv run jobs eval run                                   # the configured model
uv run jobs eval run -m google/gemini-2.5-flash -m deepseek/deepseek-chat --brief
```

A model's verdict is `no` if the digest would drop the job, `strong` if it ranks at or above
`--strong-at` (default 6.0), and `maybe` otherwise. Results are cached per model and
profile, so re-runs are free until you change `profile.md` or the prompt. `evals/` is
gitignored: the labels are personal.

## Secrets

API keys and SMTP credentials are read from environment variables named in `config.yaml`
(`api_key_env`, `username_env`, `password_env`). Locally, put them in a `.env` file next to
`config.yaml` (gitignored); variables already set in your shell take precedence:

```sh
OPENROUTER_API_KEY=sk-or-...
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-gmail-app-password
```

## Deploying on NixOS (no flakes needed)

1. Clone the repo anywhere (it's read at build time and copied into the Nix store), and put
   your instance files in your NixOS config directory:

   ```sh
   git clone https://github.com/<you>/job-pipeline ~/job-pipeline
   sudo mkdir /etc/nixos/job-pipeline   # config.yaml, profile.md, watchlist.yaml go here
   ```

   In that `config.yaml`, use `backend: openai_compatible` (the Claude CLI isn't logged in on
   the server).

2. Create the secrets file, readable by root only, kept out of the Nix store:

   ```sh
   sudo install -m 600 /dev/null /etc/job-pipeline.env
   sudoedit /etc/job-pipeline.env       # OPENROUTER_API_KEY=..., SMTP_USER=..., SMTP_PASSWORD=...
   ```

3. Add to `configuration.nix`:

   ```nix
   imports = [ /home/<you>/job-pipeline/nix/module.nix ];

   services.job-pipeline = {
     enable = true;
     configDir = ./job-pipeline;                    # a path literal: copied into the store
     environmentFile = "/etc/job-pipeline.env";     # a string: never copied into the store
     collect.schedule = "daily";                    # systemd OnCalendar syntax
     digest.schedule = "Mon 08:00";
   };
   ```

4. Build, try, then commit. This is safe over SSH: `test` activates without a boot entry,
   so a reboot or `nixos-rebuild switch --rollback` gets you back.

   ```sh
   sudo nixos-rebuild build                       # evaluates and builds (runs the test suite)
   sudo nixos-rebuild test                        # activate now, not on next boot
   sudo job-pipeline collect --dry-run            # runs as the service user with its secrets
   sudo nixos-rebuild switch
   ```

5. Operate it:

   ```sh
   systemctl list-timers 'job-pipeline-*'
   sudo systemctl start job-pipeline-collect      # run now
   journalctl -u job-pipeline-collect -e          # logs
   sudo job-pipeline scores                       # any CLI command, as the service user
   ```

   State (database, reports, evals) lives in `/var/lib/job-pipeline`. To update, `git pull`
   in the clone and rebuild.

Flake users: `inputs.job-pipeline.url = "github:<you>/job-pipeline";`, then
`imports = [ inputs.job-pipeline.nixosModules.default ];` with the same options.

## Status

- [x] Config, SQLite store, cross-source dedupe
- [x] Greenhouse / Lever / Ashby watchlist + `watchlist resolve`
- [x] Hard filters (age, title, three-way location)
- [x] HN Who is Hiring, Remotive, We Work Remotely
- [x] LLM scoring via Claude Code CLI (`backend: cli`) + `jobs scores`
- [x] OpenAI-compatible backend (OpenRouter, Ollama, …)
- [x] Ranking
- [x] Digest (markdown + HTML report), email notifier, `jobs mark`
- [x] `jobs eval` golden set
- [x] Nix package + NixOS module (works with or without flakes)
