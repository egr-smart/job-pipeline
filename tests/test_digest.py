from datetime import UTC, datetime

import pytest

from job_pipeline.config import Config, EmailNotifier
from job_pipeline.digest import build_digest
from job_pipeline.llm.scoring import ScoreResult
from job_pipeline.models import Outcome
from job_pipeline.notifiers import NotifyError, send_email
from job_pipeline.rank import Sections, build_sections
from job_pipeline.store import Store

from .conftest import make_job

NOW = datetime(2026, 11, 11, tzinfo=UTC)
SEEN = "2026-11-10T00:00:00+00:00"


def _score(**kw) -> str:
    base = dict(summary="Good fit.", gaps=[], red_flags=[], location_eligible="yes", location_note="APAC",
                salary_text=None, role_fit=8, stack_fit=6, seniority="match")
    return ScoreResult.model_validate({**base, **kw}).model_dump_json()


@pytest.fixture
def populated(cfg: Config):
    s = Store(":memory:")
    specs = [
        ("1", "Platform Engineer", {}),
        ("2", "Backend Engineer", {"location_eligible": "unclear", "location_note": "not stated"}),
        ("3", "Staff SRE", {"seniority": "under"}),
        ("4", "Infra Engineer", {"role_fit": 4, "red_flags": ["24/7 on-call"]}),
    ]
    ids = {}
    for sid, title, kw in specs:
        job_id, _ = s.upsert(make_job(source_id=sid, title=title, url=f"https://ex.com/{sid}"), SEEN)
        s.set_filter(job_id, Outcome("allow", "ok"))
        s.save_score(job_id, "h", "fake", _score(**kw))
        ids[title] = job_id
    # Same Platform Engineer role also on HN: should show as an extra link.
    s.upsert(make_job(source="hn", source_id="99", title="Platform Engineer", url="https://hn/99"), SEEN)
    return s, ids


def test_sections_and_digest(cfg, populated):
    store, ids = populated
    sections = build_sections(cfg, store, "h", "fake", unshown_only=True, now=NOW)
    assert [i.job["title"] for i in sections.top] == ["Platform Engineer", "Infra Engineer"]
    assert [i.job["title"] for i in sections.unclear] == ["Backend Engineer"]
    assert [i.job["title"] for i in sections.dropped] == ["Staff SRE"]

    d = build_digest(cfg, sections, None, now=NOW)
    assert d.subject.startswith("Job digest — 2 matches, 1 worth asking")
    assert "[Platform Engineer](https://ex.com/1)" in d.markdown
    assert "[HN](https://hn/99)" in d.markdown
    assert "Staff SRE" not in d.markdown
    assert "<h3>" in d.html and "https://ex.com/1" in d.html
    assert sorted(d.shortlisted) == sorted(
        [(ids["Platform Engineer"], "top"), (ids["Infra Engineer"], "top"), (ids["Backend Engineer"], "unclear")]
    )


def test_min_score_drops_weak_fits(cfg, populated):
    store, _ = populated
    cfg.ranking.min_score = 6.5  # Infra Engineer (role 4, one red flag) ranks ~4.8
    sections = build_sections(cfg, store, "h", "fake", unshown_only=True, now=NOW)
    assert [i.job["title"] for i in sections.top] == ["Platform Engineer"]
    assert any(i.dropped.startswith("score") for i in sections.dropped)


def test_shown_and_marked_jobs_are_excluded(cfg, populated):
    store, ids = populated
    store.record_appearances([(ids["Platform Engineer"], "top")], SEEN)
    with store.db:
        store.db.execute("INSERT INTO marks VALUES (?, 'dismissed', NULL, ?)", (ids["Infra Engineer"], SEEN))
    sections = build_sections(cfg, store, "h", "fake", unshown_only=True, now=NOW)
    assert sections.top == []
    # The terminal view still shows already-digested jobs, but never marked ones.
    everything = build_sections(cfg, store, "h", "fake", unshown_only=False, now=NOW)
    assert [i.job["title"] for i in everything.top] == ["Platform Engineer"]


def test_stale_jobs_are_excluded(cfg, populated):
    store, _ = populated
    later = datetime(2027, 1, 30, tzinfo=UTC)  # beyond active_window_days
    assert build_sections(cfg, store, "h", "fake", unshown_only=True, now=later).top == []


def test_email_requires_credentials(cfg, monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    n = EmailNotifier(type="email", smtp_host="localhost", username_env="SMTP_USER",
                      password_env="SMTP_PASSWORD", to="me@example.com")
    with pytest.raises(NotifyError, match="SMTP_USER"):
        send_email(n, build_digest(cfg, Sections(), None, now=NOW))
