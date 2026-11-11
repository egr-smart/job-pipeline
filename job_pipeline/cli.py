from pathlib import Path
from typing import Annotated

import typer

from .config import ConfigError, load_config
from .http import Http
from .llm import make_backend
from .llm.scoring import ScoreResult, prompt_hash, system_prompt
from .pipeline import collect as run_collect
from .rank import drop_reason, rank_score
from .sources.watchlist import load_watchlist, resolve_company, save_watchlist
from .store import Store

app = typer.Typer(no_args_is_help=True, help="Automated remote job search.")
watchlist_app = typer.Typer(no_args_is_help=True, help="Manage the company watchlist.")
app.add_typer(watchlist_app, name="watchlist")

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.yaml (default: $JOBS_CONFIG or ./config.yaml)")
]


def _load(config: Path | None):
    try:
        return load_config(config)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2)


@app.command()
def collect(
    config: ConfigOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fetch and filter only; no LLM calls.")] = False,
    limit: Annotated[int | None, typer.Option(help="Score at most N jobs this run (newest first).")] = None,
):
    """Fetch, dedupe, filter and score new jobs."""
    cfg = _load(config)
    store = Store(cfg.db_path)
    stats = run_collect(cfg, store, dry_run=dry_run, limit=limit, progress=typer.echo)

    failed = [s for s in stats.sources if s["error"]]
    ok = len(stats.sources) - len(failed)
    typer.echo(f"Sources: {ok}/{len(stats.sources)} ok")
    for s in failed:
        typer.secho(f"  failed  {s['name']}: {s['error']}", fg="yellow")
    r = stats.results
    typer.echo(
        f"Pipeline: {stats.fetched} fetched → {stats.new} new → "
        f"{r['allow']} allow + {r['unknown']} unknown passed, {r['deny']} denied"
    )
    if stats.deny_reasons:
        top = ", ".join(f"{k} ({v})" for k, v in stats.deny_reasons.most_common(8))
        typer.echo(f"  deny reasons: {top}")
    if dry_run:
        msg = f"Would score: {stats.to_score}"
    else:
        msg = f"Scored: {stats.scored}/{stats.to_score} with {stats.scorer_id}"
        if stats.cost_usd:
            msg += f" (≈${stats.cost_usd:.3f} at list price)"
    if stats.deferred:
        msg += f"; {stats.deferred} more left for later runs"
    typer.echo(msg)
    if stats.scoring_aborted:
        typer.secho(f"Scoring stopped: {stats.scoring_aborted}", fg="yellow")


@app.command()
def scores(
    config: ConfigOpt = None,
    show_dropped: Annotated[bool, typer.Option("--dropped", help="Also list jobs dropped by ranking.")] = False,
):
    """Show scored jobs for the current profile and model, ranked as the digest would."""
    cfg = _load(config)
    if cfg.llm is None:
        typer.secho("No llm configured.", fg="red", err=True)
        raise typer.Exit(2)
    store = Store(cfg.db_path)
    backend = make_backend(cfg.llm.scoring)
    phash = prompt_hash(system_prompt(cfg.read_profile(), cfg.home_country))

    sections: dict[str, list] = {"Top matches": [], "Worth asking (location unclear)": [], "Dropped": []}
    for row in store.scored_jobs(phash, backend.id):
        if row["mark"]:
            continue
        result = ScoreResult.model_validate_json(row["result_json"])
        dropped = drop_reason(result)
        key = "Dropped" if dropped else (
            "Top matches" if result.location_eligible == "yes" else "Worth asking (location unclear)"
        )
        sections[key].append((rank_score(result, cfg.ranking), row, result, dropped))

    for title, items in sections.items():
        if title == "Dropped" and not show_dropped:
            typer.echo(f"\n({len(items)} dropped; show with --dropped)")
            continue
        typer.secho(f"\n## {title} ({len(items)})", bold=True)
        for score, row, r, dropped in sorted(items, key=lambda i: -i[0]):
            typer.secho(f"\n#{row['id']} {row['company']} — {row['title']}   [score {score}]", bold=True)
            typer.echo(f"   Location: {row['location_raw']!r} → {r.location_eligible}: {r.location_note}")
            if r.salary_text or row["salary_raw"]:
                typer.echo(f"   Salary: {r.salary_text or row['salary_raw']}")
            typer.echo(f"   Why: {r.summary}")
            if r.gaps:
                typer.echo(f"   Gaps: {'; '.join(r.gaps)}")
            if r.red_flags:
                typer.echo(f"   Flags: {'; '.join(r.red_flags)}")
            line = f"   role {r.role_fit} · stack {r.stack_fit} · seniority {r.seniority}"
            typer.echo(line + (f"   DROPPED: {dropped}" if dropped else ""))


@app.command()
def jobs(
    config: ConfigOpt = None,
    result: Annotated[str, typer.Option(help="allow | unknown | deny")] = "allow",
    limit: int = 50,
):
    """List stored jobs by filter result, with the filter's reason. Useful for tuning phrase lists."""
    cfg = _load(config)
    store = Store(cfg.db_path)
    rows = store.db.execute(
        """
        SELECT j.id, j.company, j.title, j.location_raw, j.filter_reason,
               (SELECT url FROM listings l WHERE l.job_id = j.id LIMIT 1) AS url
        FROM jobs j WHERE j.filter_result = ? ORDER BY j.last_seen DESC, j.id DESC LIMIT ?
        """,
        (result, limit),
    ).fetchall()
    for r in rows:
        typer.echo(f"#{r['id']:<5} {r['company']} — {r['title']}")
        typer.echo(f"       location: {r['location_raw']!r}  →  {r['filter_reason']}")
        typer.echo(f"       {r['url']}")


@watchlist_app.command("resolve")
def watchlist_resolve(
    config: ConfigOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="Re-check companies that already have an ATS/slug.")] = False,
):
    """Find each company's ATS (Greenhouse/Lever/Ashby) and board slug; write them back."""
    cfg = _load(config)
    if not cfg.sources.watchlist:
        typer.secho("No watchlist configured (sources.watchlist).", fg="red", err=True)
        raise typer.Exit(2)
    path = cfg.path(cfg.sources.watchlist)
    wl = load_watchlist(path)
    unresolved = []
    with Http(min_interval=0.2) as http:
        for c in wl.companies:
            if c.ats and c.slug and not all_:
                continue
            hits = resolve_company(http, c)
            if not hits:
                unresolved.append(c.name)
                typer.secho(f"  ✗ {c.name}", fg="yellow")
                continue
            c.ats, c.slug, count = hits[0]
            note = "  (empty board; verify slug)" if count == 0 else ""
            typer.echo(f"  ✓ {c.name}: {c.ats}/{c.slug} ({count} jobs){note}")
            if len(hits) > 1:
                others = ", ".join(f"{a}/{s} ({n})" for a, s, n in hits[1:])
                typer.secho(f"      ambiguous, also found: {others}; check it's the right company", fg="yellow")
    save_watchlist(path, wl)
    if unresolved:
        typer.secho(
            f"{len(unresolved)} unresolved (custom careers site or unusual slug); set ats/slug by hand "
            f"or remove: {', '.join(unresolved)}",
            fg="yellow",
        )
