"""The `collect` stage: fetch every source, store, dedupe and filter (then score, once wired)."""

from collections import Counter
from dataclasses import dataclass, field

from .config import Config
from .filter import Filter
from .http import Http
from .sources import SourceResult
from .sources.watchlist import fetch_watchlist, load_watchlist
from .store import Store, now_iso


@dataclass
class CollectStats:
    sources: list[dict] = field(default_factory=list)
    fetched: int = 0
    new: int = 0
    results: Counter = field(default_factory=Counter)
    deny_reasons: Counter = field(default_factory=Counter)
    to_score: int = 0
    deferred: int = 0

    def as_dict(self) -> dict:
        return {
            "sources": self.sources,
            "fetched": self.fetched,
            "new": self.new,
            "results": dict(self.results),
            "deny_reasons": dict(self.deny_reasons),
            "to_score": self.to_score,
            "deferred": self.deferred,
        }


def fetch_all(cfg: Config, http: Http) -> list[SourceResult]:
    results: list[SourceResult] = []
    if cfg.sources.watchlist:
        results += fetch_watchlist(http, load_watchlist(cfg.path(cfg.sources.watchlist)))
    # TODO: HN Who is Hiring, Remotive, We Work Remotely
    return results


def collect(cfg: Config, store: Store, *, dry_run: bool) -> CollectStats:
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

    passed = stats.results["allow"] + stats.results["unknown"]
    stats.to_score = min(passed, cfg.limits.max_jobs_per_run)
    stats.deferred = passed - stats.to_score

    if not dry_run:
        # TODO: LLM scoring of passed, unscored jobs (newest first, capped).
        pass

    store.record_run("collect-dry-run" if dry_run else "collect", started, stats.as_dict())
    return stats
