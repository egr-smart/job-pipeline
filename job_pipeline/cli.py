from pathlib import Path
from typing import Annotated

import typer

from .config import ConfigError, load_config
from .http import Http
from .pipeline import collect as run_collect
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
):
    """Fetch, dedupe, filter and score new jobs."""
    cfg = _load(config)
    store = Store(cfg.db_path)
    stats = run_collect(cfg, store, dry_run=dry_run)

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
    msg = f"Would score: {stats.to_score}" if dry_run else f"To score: {stats.to_score}"
    if stats.deferred:
        msg += f" (+{stats.deferred} deferred by max_jobs_per_run)"
    typer.echo(msg)
    if not dry_run:
        typer.secho("LLM scoring not implemented yet; run with --dry-run.", fg="yellow")


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
