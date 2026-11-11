"""The `collect` stage: fetch every source, store, dedupe, filter, then score with the LLM."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config
from .filter import Filter
from .http import Http
from .llm import LLMError, make_backend
from .llm.scoring import job_message, prompt_hash, score_job, system_prompt
from .sources import SourceResult
from .sources.boards import fetch_hn, fetch_remotive, fetch_wwr
from .sources.watchlist import fetch_watchlist, load_watchlist
from .store import Store, now_iso

# Stop scoring after this many failures in a row; something is wrong beyond one job.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class CollectStats:
    sources: list[dict] = field(default_factory=list)
    fetched: int = 0
    new: int = 0
    results: Counter = field(default_factory=Counter)
    deny_reasons: Counter = field(default_factory=Counter)
    to_score: int = 0
    deferred: int = 0
    scored: int = 0
    score_failures: list[str] = field(default_factory=list)
    scoring_aborted: str | None = None
    cost_usd: float = 0.0
    scorer_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "sources": self.sources,
            "fetched": self.fetched,
            "new": self.new,
            "results": dict(self.results),
            "deny_reasons": dict(self.deny_reasons),
            "to_score": self.to_score,
            "deferred": self.deferred,
            "scored": self.scored,
            "score_failures": self.score_failures,
            "scoring_aborted": self.scoring_aborted,
            "cost_usd": round(self.cost_usd, 4),
            "scorer_id": self.scorer_id,
        }


def fetch_all(cfg: Config, http: Http) -> list[SourceResult]:
    results: list[SourceResult] = []
    if cfg.sources.watchlist:
        results += fetch_watchlist(http, load_watchlist(cfg.path(cfg.sources.watchlist)))
    src = cfg.sources
    if src.hn_who_is_hiring.enabled:
        results.append(fetch_hn(http, src.hn_who_is_hiring.threads))
    if src.remotive.enabled:
        results.append(fetch_remotive(http))
    if src.weworkremotely.enabled:
        results += fetch_wwr(http, src.weworkremotely.categories)
    return results


def collect(
    cfg: Config,
    store: Store,
    *,
    dry_run: bool,
    limit: int | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> CollectStats:
    started = now_iso()
    stats = CollectStats()
    check = Filter(cfg)
    with Http() as http:
        fetched = fetch_all(cfg, http)

    seen_this_run: set[int] = set()
    for src in fetched:
        stats.sources.append({"name": src.name, "jobs": len(src.jobs), "error": src.error})
        for job in src.jobs:
            stats.fetched += 1
            job_id, is_new = store.upsert(job, started)
            stats.new += is_new
            if job_id in seen_this_run:
                continue
            seen_this_run.add(job_id)
            # Re-filter every job seen this run so edits to the phrase lists take effect.
            outcome = check(store.get_job(job_id))
            store.set_filter(job_id, outcome)
            stats.results[outcome.result] += 1
            if outcome.result == "deny":
                stats.deny_reasons[outcome.reason.split(":")[0]] += 1

    if cfg.llm is None:
        if not dry_run:
            stats.scoring_aborted = "no llm configured"
    else:
        try:
            backend = make_backend(cfg.llm.scoring)
        except NotImplementedError as e:
            stats.scoring_aborted = str(e)
            store.record_run("collect-dry-run" if dry_run else "collect", started, stats.as_dict())
            return stats
        system = system_prompt(cfg.read_profile(), cfg.home_country)
        phash = prompt_hash(system)
        stats.scorer_id = backend.id
        pending = store.jobs_to_score(started, phash, backend.id, resurface=cfg.ranking.resurface)
        cap = min(cfg.limits.max_jobs_per_run, limit) if limit is not None else cfg.limits.max_jobs_per_run
        batch = pending[:cap]
        stats.to_score, stats.deferred = len(batch), len(pending) - len(batch)
        if not dry_run:
            _score(store, backend, system, phash, batch, stats, progress)

    store.record_run("collect-dry-run" if dry_run else "collect", started, stats.as_dict())
    return stats


def _score(store, backend, system, phash, job_ids, stats, progress) -> None:
    consecutive = 0
    for n, job_id in enumerate(job_ids, 1):
        job = store.get_job(job_id)
        row = store.db.execute("SELECT filter_result, filter_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()
        label = f"[{n}/{len(job_ids)}] #{job_id} {job.company} — {job.title}"
        try:
            result, cost = score_job(backend, system, job_message(job, row[0], row[1]))
        except LLMError as e:
            stats.score_failures.append(f"#{job_id}: {e}")
            progress(f"{label}  FAILED: {e}")
            consecutive += 1
            if e.fatal or consecutive >= MAX_CONSECUTIVE_FAILURES:
                stats.scoring_aborted = str(e)
                return
            continue
        consecutive = 0
        store.save_score(job_id, phash, backend.id, result.model_dump_json())
        stats.scored += 1
        stats.cost_usd += cost or 0
        progress(f"{label}  role {result.role_fit} · stack {result.stack_fit} · {result.seniority} · loc {result.location_eligible}")
