"""Turn LLM sub-scores into a rank. Done in code so weights are tunable and model-independent."""

from .config import Ranking
from .llm.scoring import ScoreResult


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
