from datetime import UTC, datetime, timedelta

import pytest

from job_pipeline.filter import Filter, check_title
from job_pipeline.filter.location import Vocab, classify_location

from .conftest import make_job

VOCAB = Vocab.load(home_country="AU")


@pytest.mark.parametrize(
    "location, workplace, title, description, expected",
    [
        # Explicit regions covering AU
        ("Remote, Global", None, "", "", "allow"),
        ("Remote - APAC", None, "", "", "allow"),
        ("Remote (Asia-Pacific)", None, "", "", "allow"),
        ("Australia (Remote)", None, "", "", "allow"),
        ("Remote; ANZ", None, "", "", "allow"),
        ("Home based - Worldwide", None, "", "", "allow"),
        ("Remote - US; Remote - Australia", None, "", "", "allow"),
        ("Singapore; Australia", None, "Senior SRE (APJ-Remote)", "", "allow"),
        ("Sydney", "remote", "", "", "allow"),
        # Explicit regions excluding AU
        ("Remote, United States", None, "", "", "deny"),
        ("Netherlands (Remote)", None, "", "", "deny"),
        ("Remote - EMEA", None, "", "", "deny"),
        ("Remote, AMER", None, "", "", "deny"),
        ("Remote - Anywhere in the US", None, "", "", "deny"),
        ("Europe or US", None, "", "", "deny"),
        ("Remote", None, "SRE, Infrastructure Platforms — UK", "", "deny"),
        # Not remote
        ("Warsaw, Poland - Hybrid", None, "", "", "deny"),
        ("Sydney", "hybrid", "", "", "deny"),
        ("Gold Coast office", None, "", "", "deny"),
        # Timezone requirements are never a deny reason
        ("Remote (US time zones)", None, "", "", "unknown"),
        ("Remote, APAC (overlap with US Pacific)", None, "", "", "allow"),
        # Unknown: remote with no region, no tells
        ("Remote", None, "", "We build databases.", "unknown"),
        ("", None, "", "", "unknown"),
        # Home country named but remote not stated -> LLM judges
        ("Australia", None, "", "", "unknown"),
        # Description-based decisions when no region is named
        ("Remote", None, "", "We hire anywhere via Deel.", "allow"),
        ("Remote", None, "", "Benefits include a 401(k) match.", "deny"),
        ("Remote", None, "", "Must be authorized to work in the United States.", "deny"),
        ("Remote, APAC", None, "", "Requires an active security clearance.", "deny"),
    ],
)
def test_location(location, workplace, title, description, expected):
    job = make_job(
        location_raw=location, workplace=workplace, title=title or "Platform Engineer",
        description_text=description,
    )
    outcome = classify_location(job, "AU", VOCAB)
    assert outcome.result == expected, outcome.reason


def test_us_tells_ignored_for_us_user():
    job = make_job(location_raw="Remote", description_text="401(k) match")
    assert classify_location(job, "US", Vocab.load(home_country="US")).result == "unknown"


def test_extra_allowed_regions():
    vocab = Vocab.load(extra_allowed_regions=["Down Under"], home_country="AU")
    job = make_job(location_raw="Remote - Down Under")
    assert classify_location(job, "AU", vocab).result == "allow"


@pytest.mark.parametrize(
    "title, ok",
    [
        ("Senior Platform Engineer", True),
        ("Site Reliability Engineer (SRE)", True),
        ("Platform Sales Lead", False),  # exclude wins
        ("Engineering Manager, Infrastructure", False),
        ("Senior Frontend Engineer", False),  # no include keyword
        ("Platformer", False),  # whole words only
    ],
)
def test_title(title, ok):
    assert (check_title(title, ["platform", "infrastructure", "sre"], ["sales", "manager"]) is None) == ok


def test_age_applies_to_aggregators_only(cfg):
    now = datetime(2026, 11, 9, tzinfo=UTC)
    old = now - timedelta(days=90)
    check = Filter(cfg)
    ats_job = make_job(source="greenhouse", location_raw="Remote, APAC", posted_at=old)
    board_job = make_job(source="remotive", location_raw="Remote, APAC", posted_at=old)
    assert check(ats_job, now).result == "allow"
    assert check(board_job, now).reason.startswith("older than")
