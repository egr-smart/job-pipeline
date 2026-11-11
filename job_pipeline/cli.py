import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from .config import ConfigError, EmailNotifier, FileNotifier, load_config
from .digest import build_digest
from .evals import EvalSet, format_report, run_eval
from .http import Http
from .llm import make_backend
from .llm.scoring import prompt_hash, system_prompt
from .notifiers import NotifyError, send_email, write_report
from .pipeline import collect as run_collect
from .rank import build_sections
from .sources.watchlist import load_watchlist, resolve_company, save_watchlist
from .store import Store, now_iso

app = typer.Typer(no_args_is_help=True, help="Automated remote job search.")
watchlist_app = typer.Typer(no_args_is_help=True, help="Manage the company watchlist.")
app.add_typer(watchlist_app, name="watchlist")
eval_app = typer.Typer(no_args_is_help=True, help="Hand-labelled golden set for checking the scorer.")
app.add_typer(eval_app, name="eval")

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


def _scoring_ids(cfg) -> tuple[str, str]:
    """(profile_hash, scorer_id) for the configured profile, prompt and model."""
    if cfg.llm is None:
        typer.secho("No llm configured.", fg="red", err=True)
        raise typer.Exit(2)
    backend = make_backend(cfg.llm.scoring)
    return prompt_hash(system_prompt(cfg.read_profile(), cfg.home_country)), backend.id


@app.command()
def scores(
    config: ConfigOpt = None,
    show_dropped: Annotated[bool, typer.Option("--dropped", help="Also list jobs dropped by ranking.")] = False,
):
    """Show every scored job for the current profile and model, ranked, in the terminal."""
    cfg = _load(config)
    store = Store(cfg.db_path)
    sections = build_sections(cfg, store, *_scoring_ids(cfg), unshown_only=False)
    named = {
        "Top matches": sections.top,
        "Worth asking (location unclear)": sections.unclear,
        "Dropped": sections.dropped,
    }
    for title, items in named.items():
        if title == "Dropped" and not show_dropped:
            typer.echo(f"\n({len(items)} dropped; show with --dropped)")
            continue
        typer.secho(f"\n## {title} ({len(items)})", bold=True)
        for item in items:
            j, r = item.job, item.result
            shown = "  (already in a digest)" if j["shown"] else ""
            typer.secho(f"\n#{j['id']} {j['company']} — {j['title']}   [score {item.score}]{shown}", bold=True)
            typer.echo(f"   {item.links[0][1]}")
            typer.echo(f"   Location: {j['location_raw']!r} → {r.location_eligible}: {r.location_note}")
            if r.salary_text or j["salary_raw"]:
                typer.echo(f"   Salary: {r.salary_text or j['salary_raw']}")
            typer.echo(f"   Why: {r.summary}")
            if r.gaps:
                typer.echo(f"   Gaps: {'; '.join(r.gaps)}")
            if r.red_flags:
                typer.echo(f"   Flags: {'; '.join(r.red_flags)}")
            line = f"   role {r.role_fit} · stack {r.stack_fit} · seniority {r.seniority}"
            typer.echo(line + (f"   DROPPED: {item.dropped}" if item.dropped else ""))


@app.command()
def digest(
    config: ConfigOpt = None,
    preview: Annotated[
        bool, typer.Option("--preview", help="Write the report only: no email, and jobs stay unshown.")
    ] = False,
    include_dropped: Annotated[bool, typer.Option("--dropped", help="Append dropped jobs to the report.")] = False,
    open_report: Annotated[bool, typer.Option("--open", help="Open the HTML report afterwards.")] = False,
):
    """Build the shortlist from unshown jobs and send it to the configured notifiers."""
    cfg = _load(config)
    store = Store(cfg.db_path)
    sections = build_sections(cfg, store, *_scoring_ids(cfg), unshown_only=True)
    last_run = store.last_run("collect") or store.last_run("collect-dry-run")
    d = build_digest(cfg, sections, last_run, include_dropped=include_dropped)

    file_notifiers = [n for n in cfg.notifiers if isinstance(n, FileNotifier)] or [FileNotifier(type="file")]
    report = write_report(cfg, file_notifiers[0], d, suffix="-preview" if preview else "")
    typer.echo(d.subject)
    typer.echo(f"Report: {report}")
    if open_report:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(report)], check=False)
    if preview:
        return

    for n in cfg.notifiers:
        if isinstance(n, EmailNotifier):
            try:
                send_email(n, d)
                typer.echo(f"Emailed {n.to}")
            except NotifyError as e:
                typer.secho(f"Email failed: {e}", fg="red", err=True)
    # Recorded even if email failed: the report file has them, and a retry shouldn't spam.
    store.record_appearances(d.shortlisted, now_iso())


@app.command()
def mark(
    job_id: int,
    status: Annotated[str, typer.Argument(help="applied | dismissed")],
    reason: Annotated[str | None, typer.Option(help="Optional note, e.g. 'actually US-only'.")] = None,
    config: ConfigOpt = None,
):
    """Mark a job so it never appears in a digest again."""
    if status not in ("applied", "dismissed"):
        typer.secho("status must be 'applied' or 'dismissed'", fg="red", err=True)
        raise typer.Exit(2)
    store = Store(_load(config).db_path)
    row = store.db.execute("SELECT company, title FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        typer.secho(f"No job #{job_id}", fg="red", err=True)
        raise typer.Exit(1)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO marks VALUES (?, ?, ?, ?)", (job_id, status, reason, now_iso()))
    typer.echo(f"#{job_id} {row['company']} — {row['title']}: {status}")


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


@eval_app.command("add")
def eval_add(
    job_id: int,
    verdict: Annotated[str, typer.Argument(help="strong | maybe | no: would you want this in your digest?")],
    location: Annotated[str | None, typer.Option(help="yes | no | unclear: can you be hired from your country?")] = None,
    note: Annotated[str | None, typer.Option(help="Why; shown next to disagreements.")] = None,
    config: ConfigOpt = None,
):
    """Label a stored job and snapshot it into the golden set."""
    if verdict not in ("strong", "maybe", "no") or location not in (None, "yes", "no", "unclear"):
        typer.secho("verdict: strong|maybe|no; --location: yes|no|unclear", fg="red", err=True)
        raise typer.Exit(2)
    cfg = _load(config)
    try:
        label = EvalSet(cfg.path(cfg.evals_dir)).add(Store(cfg.db_path), job_id, verdict, location, note)
    except KeyError:
        typer.secho(f"No job #{job_id}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"#{label.id} {label.company} — {label.title}: {label.verdict}"
               + (f", location {label.location}" if label.location else ""))


@eval_app.command("list")
def eval_list(config: ConfigOpt = None):
    """Show the labelled jobs."""
    cfg = _load(config)
    labels = EvalSet(cfg.path(cfg.evals_dir)).labels()
    for l in labels:
        loc = f" loc={l.location}" if l.location else ""
        typer.echo(f"#{l.id:<5} {l.verdict:<6}{loc:<13} {l.company} — {l.title[:70]}" + (f"  ({l.note})" if l.note else ""))
    counts = {v: sum(l.verdict == v for l in labels) for v in ("strong", "maybe", "no")}
    typer.echo(f"{len(labels)} labelled: " + ", ".join(f"{n} {v}" for v, n in counts.items()))


@eval_app.command("run")
def eval_run(
    config: ConfigOpt = None,
    models: Annotated[
        list[str] | None,
        typer.Option("--model", "-m", help="Model(s) to evaluate on the configured backend; repeatable. Default: configured model."),
    ] = None,
    strong_at: Annotated[float, typer.Option(help="Score at or above which the model counts as 'strong'.")] = 6.0,
    brief: Annotated[bool, typer.Option("--brief", help="Agreement numbers only.")] = False,
):
    """Score the golden set and compare with your labels. Results are cached per model + profile."""
    cfg = _load(config)
    if cfg.llm is None:
        typer.secho("No llm configured.", fg="red", err=True)
        raise typer.Exit(2)
    evals = EvalSet(cfg.path(cfg.evals_dir))
    if not evals.labels():
        typer.secho("No labels yet. Add some with `jobs eval add <job_id> strong|maybe|no`.", fg="yellow")
        raise typer.Exit(1)
    reports = []
    for model in models or [None]:
        backend_cfg = cfg.llm.scoring if model is None else cfg.llm.scoring.model_copy(update={"model": model})
        backend = make_backend(backend_cfg)
        typer.secho(f"\nEvaluating {backend.id}", bold=True)
        report = run_eval(cfg, evals, backend, strong_at=strong_at, progress=typer.echo)
        reports.append(report)
        typer.echo("\n" + format_report(report, verbose=not brief))
    if len(reports) > 1:
        typer.secho("\nSummary", bold=True)
        for r in sorted(reports, key=lambda r: (-r.verdict_agreement[0], len(r.serious_misses))):
            a, t = r.verdict_agreement
            typer.echo(f"  {r.scorer_id:<45} verdict {a}/{t}  serious misses {len(r.serious_misses)}"
                       + (f"  ≈${r.cost_usd:.3f}" if r.cost_usd else ""))
