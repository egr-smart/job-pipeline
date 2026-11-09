"""Cheap hard filters run before any LLM call: age -> title -> location."""

from datetime import UTC, datetime, timedelta

from ..config import Config
from ..models import Outcome, RawJob
from ..text import contains_phrase, normalize_words
from .location import Vocab, classify_location


# A company's own ATS board only lists open roles, however old the posting date. The age
# limit is for aggregators, where nothing tells us a listing has closed.
AGE_EXEMPT_SOURCES = {"greenhouse", "lever", "ashby"}


def check_age(job: RawJob, max_age_days: int, now: datetime) -> Outcome | None:
    if job.posted_at is None or job.source in AGE_EXEMPT_SOURCES:
        return None
    posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=UTC)
    if now - posted > timedelta(days=max_age_days):
        return Outcome("deny", f"older than {max_age_days} days")
    return None


def check_title(title: str, include: list[str], exclude: list[str]) -> Outcome | None:
    t = normalize_words(title)
    if hit := next((p for p in exclude if contains_phrase(t, p)), None):
        return Outcome("deny", f"title excludes: {hit}")
    if not any(contains_phrase(t, p) for p in include):
        return Outcome("deny", "title matches no include keyword")
    return None


class Filter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vocab = Vocab.load(cfg.location.extra_allowed_regions, cfg.home_country)

    def __call__(self, job: RawJob, now: datetime | None = None) -> Outcome:
        now = now or datetime.now(UTC)
        return (
            check_age(job, self.cfg.limits.max_age_days, now)
            or check_title(job.title, self.cfg.titles.include, self.cfg.titles.exclude)
            or classify_location(job, self.cfg.home_country, self.vocab)
        )
