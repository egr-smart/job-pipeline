from job_pipeline.sources.boards import parse_hn_comment, parse_remotive, parse_wwr

from .conftest import FIXTURES, load_fixture


def test_hn_header_becomes_title_and_location():
    comments = load_fixture("hn/thread.json")["children"]
    jobs = [parse_hn_comment(c) for c in comments]
    first = jobs[0]
    assert first.source == "hn" and first.company == "Simple Analytics"
    assert first.title.startswith("DevOps Engineer | REMOTE (worldwide")
    assert first.location_raw.startswith("Simple Analytics | DevOps Engineer")
    assert first.url == "https://news.ycombinator.com/item?id=50338732"
    assert first.posted_at is not None
    assert "&amp;" not in jobs[1].title  # entities decoded
    assert jobs[4] is None  # deleted comment
    assert jobs[5] is None  # no pipe-separated header


def test_remotive():
    jobs = parse_remotive(load_fixture("remotive/jobs.json"))
    assert jobs[0].source == "remotive" and jobs[0].workplace == "remote"
    assert jobs[0].location_raw == "Worldwide"
    assert jobs[0].posted_at.tzinfo is not None
    assert jobs[0].url.startswith("https://remotive.com/")


def test_wwr_country_list_beats_region():
    jobs = parse_wwr((FIXTURES / "weworkremotely/backend.xml").read_text())
    vertican, smartsheet = jobs
    assert vertican.company == "Vertican"
    assert vertican.title == "Senior Applied AI Engineer, Document Intelligence"
    assert vertican.location_raw.startswith("Bolivia")  # not "Anywhere in the World"
    assert "🇧" not in vertican.location_raw
    assert smartsheet.location_raw == "Anywhere in the World"
    assert smartsheet.workplace == "remote"
