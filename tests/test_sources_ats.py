from job_pipeline.sources.ats import parse_ashby, parse_greenhouse, parse_lever
from job_pipeline.sources.watchlist import slug_candidates

from .conftest import load_fixture


def test_greenhouse():
    jobs = parse_greenhouse(load_fixture("greenhouse/mixed.json"), "Acme")
    first = jobs[0]
    assert first.source == "greenhouse" and first.company == "Acme"
    assert first.location_raw == "Remote, United States"
    assert first.salary_raw == "USD 139.2k–235.2k (United States Salary Range)"
    assert first.posted_at is not None and first.posted_at.tzinfo is not None
    assert "<" not in first.description_text and "&lt;" not in first.description_text
    assert jobs[1].salary_raw is None


def test_lever():
    jobs = parse_lever(load_fixture("lever/palantir.json"), "Palantir")
    assert [j.workplace for j in jobs] == ["hybrid", "remote"]
    assert jobs[0].location_raw == "Singapore, Singapore"
    assert jobs[0].url.startswith("https://jobs.lever.co/palantir/")
    assert jobs[0].posted_at is not None


def test_ashby():
    jobs = parse_ashby(load_fixture("ashby/supabase.json"), "Supabase")
    assert jobs[0].location_raw == "Remote, Global"
    assert jobs[0].workplace is None
    assert jobs[1].workplace == "remote"
    assert jobs[0].description_text


def test_slug_candidates():
    assert slug_candidates("Grafana Labs")[:3] == ["grafanalabs", "grafana-labs", "grafana"]
    assert "cockroach" in slug_candidates("Cockroach Labs")
    assert slug_candidates("Fly.io")[0] == "flyio"
