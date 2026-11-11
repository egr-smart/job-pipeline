"""Three-way location eligibility: allow / deny / unknown (unknown goes to the LLM)."""

import re
from dataclasses import dataclass
from importlib import resources

import yaml

from ..models import Outcome, RawJob
from ..text import contains_phrase, normalize_words

# "US time zones", "UTC+8", "overlap with PST" etc. describe working hours, not residence.
_TIMEZONE_RE = re.compile(
    r"\b(?:[a-z.]+ ){0,2}time ?zones?\b|\b(?:utc|gmt) ?[+-]? ?\d*(?::\d\d)?\b|\boverlap\b[^;()]*"
)
# "Anywhere in the US" is a US restriction, not "anywhere".
_ANYWHERE_IN_RE = re.compile(r"\banywhere in (?:the )?")


@dataclass
class Vocab:
    places: list[tuple[str, frozenset[str]]]  # (phrase, covered country codes; "*" = all)
    not_remote: list[str]
    remote: list[str]
    deny_in_description: list[str]
    international_hiring: list[str]
    country_tells: dict[str, list[str]]

    @classmethod
    def load(cls, extra_allowed_regions: list[str] = (), home_country: str = "") -> "Vocab":
        data = resources.files("job_pipeline.data")
        regions = yaml.safe_load(data.joinpath("regions.yaml").read_text())
        phrases = yaml.safe_load(data.joinpath("phrases.yaml").read_text())
        places = [
            (name, frozenset([code])) for code, names in regions["countries"].items() for name in names
        ]
        places += [
            (name, frozenset(r["countries"])) for r in regions["regions"] for name in r["names"]
        ]
        places += [(name, frozenset([home_country])) for name in extra_allowed_regions]
        # Longest phrases first so "south korea" wins over "korea" in reasons.
        places.sort(key=lambda p: -len(p[0]))
        return cls(
            places=places,
            not_remote=phrases["not_remote"],
            remote=phrases["remote"],
            deny_in_description=phrases["deny_in_description"],
            international_hiring=phrases["international_hiring"],
            country_tells=phrases["country_tells"],
        )


def _first(text: str, phrases: list[str]) -> str | None:
    return next((p for p in phrases if contains_phrase(text, p)), None)


def classify_location(job: RawJob, home: str, vocab: Vocab) -> Outcome:
    desc = normalize_words(job.description_text)
    loc = normalize_words(job.location_raw)

    if hit := _first(desc, vocab.deny_in_description):
        return Outcome("deny", f"description says: {hit}")

    title = normalize_words(job.title)
    remote = (
        job.workplace == "remote"
        or _first(loc, vocab.remote) is not None
        or contains_phrase(title, "remote")  # e.g. "Senior SRE (APJ-Remote)"
    )
    if not remote:
        if job.workplace in ("hybrid", "onsite"):
            return Outcome("deny", f"workplace: {job.workplace}")
        if hit := _first(loc, vocab.not_remote):
            return Outcome("deny", f"not remote: {hit}")

    named = _places(vocab, loc)
    if not named and remote:
        # "Remote" with the region in the title instead, e.g. "SRE, Infrastructure — UK".
        named = _places(vocab, title)

    if named:
        covered = [p for p, covers in named if "*" in covers or home in covers]
        if not covered:
            return Outcome("deny", f"region excludes {home}: {', '.join(p for p, _ in named[:3])}")
        if remote:
            return Outcome("allow", f"region includes {home}: {covered[0]}")
        # e.g. Elastic lists bare countries ("Australia") for roles that are remote in-country.
        return Outcome("unknown", f"in {home} ({covered[0]}), remote not stated")
    if not remote:
        return Outcome("deny", f"not marked remote: {job.location_raw!r}") if loc else Outcome(
            "unknown", "no location stated"
        )

    if hit := _first(desc, vocab.international_hiring):
        return Outcome("allow", f"no region stated; international hiring: {hit}")
    for country, tells in vocab.country_tells.items():
        if country != home and (hit := _first(desc, tells)):
            return Outcome("deny", f"no region stated; looks {country}-only: {hit}")
    return Outcome("unknown", "remote, no region stated")


def _places(vocab: Vocab, text: str) -> list[tuple[str, frozenset[str]]]:
    text = _ANYWHERE_IN_RE.sub("", _TIMEZONE_RE.sub(" ", text))
    return [(phrase, covers) for phrase, covers in vocab.places if contains_phrase(text, phrase)]
