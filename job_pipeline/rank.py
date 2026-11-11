"""Turn LLM sub-scores into a rank. Done in code so weights are tunable and model-independent."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config, Ranking
from .llm.scoring import ScoreResult
from .store import Store


def drop_reason(r: ScoreResult) -> str | None:
    if r.location_eligible == "no":
        return "location: not eligible"
    if r.seniority == "under":
        return "seniority: under-qualified"
    return None


def rank_score(r: ScoreResult, cfg: Ranking) -> float:
    w = cfg.weights
    return round(
        w.role_fit * r.role_fit
        + w.stack_fit * r.stack_fit
        + w.seniority * cfg.seniority_scores.get(r.seniority, 0)
        - cfg.red_flag_penalty * len(r.red_flags),
        2,
    )


@dataclass
class Item:
    score: float
    job: sqlite3.Row
    result: ScoreResult
    links: list[tuple[str, str]]  # (source, url), preferred first
    dropped: str | None = None


@dataclass
class Sections:
    top: list[Item] = field(default_factory=list)
    unclear: list[Item] = field(default_factory=list)
    dropped: list[Item] = field(default_factory=list)


def build_sections(
    cfg: Config, store: Store, profile_hash: str, scorer_id: str, *, unshown_only: bool,
    now: datetime | None = None,
) -> Sections:
    """Every scored, still-active, unmarked job for this profile+model, sorted into sections."""
    now = now or datetime.now(UTC)
    active_since = (now - timedelta(days=cfg.limits.active_window_days)).isoformat(timespec="seconds")
    out = Sections()
    for row in store.scored_jobs(profile_hash, scorer_id):
        if row["mark"] or row["last_seen"] < active_since:
            continue
        if unshown_only and row["shown"] and not cfg.ranking.resurface:
            continue
        result = ScoreResult.model_validate_json(row["result_json"])
        links = [(l["source"], l["url"]) for l in store.listings(row["id"])]
        score = rank_score(result, cfg.ranking)
        dropped = drop_reason(result) or (
            f"score {score} below min_score {cfg.ranking.min_score}" if score < cfg.ranking.min_score else None
        )
        item = Item(score, row, result, links, dropped)
        if item.dropped:
            out.dropped.append(item)
        elif result.location_eligible == "yes":
            out.top.append(item)
        else:
            out.unclear.append(item)
    for items in (out.top, out.unclear, out.dropped):
        items.sort(key=lambda i: -i.score)
    return out
