from job_pipeline.models import Outcome
from job_pipeline.store import Store, fingerprint

from .conftest import make_job


def test_fingerprint_normalises():
    assert fingerprint("Grafana Labs, Inc.", "Sr. Platform Engineer (Remote)") == fingerprint(
        "Grafana Labs", "Senior Platform Engineer"
    )
    assert fingerprint("Acme", "Senior SRE") != fingerprint("Acme", "SRE")


def test_cross_source_merge_keeps_richest_fields():
    s = Store(":memory:")
    a_id, a_new = s.upsert(make_job(source="remotive", source_id="r1", url="https://remotive/1",
                                    description_text="short"))
    b_id, b_new = s.upsert(make_job(source="greenhouse", source_id="g1", url="https://gh/1",
                                    title="Sr. Platform Engineer", description_text="much longer text",
                                    salary_raw="AUD 200k"))
    assert (a_new, b_new) == (True, False)
    assert a_id == b_id
    job = s.get_job(a_id)
    assert job.description_text == "much longer text"
    assert job.salary_raw == "AUD 200k"
    assert job.url == "https://gh/1"  # company ATS link preferred


def test_same_source_postings_never_merge():
    s = Store(":memory:")
    a, _ = s.upsert(make_job(source_id="1", location_raw="Remote - EMEA"))
    b, _ = s.upsert(make_job(source_id="2", location_raw="Remote - APAC"))
    assert a != b


def test_reupsert_is_idempotent():
    s = Store(":memory:")
    a, new1 = s.upsert(make_job(), "2026-11-01T00:00:00+00:00")
    b, new2 = s.upsert(make_job(), "2026-11-08T00:00:00+00:00")
    assert a == b and new1 and not new2
    row = s.db.execute("SELECT first_seen, last_seen FROM jobs").fetchone()
    assert row["first_seen"] < row["last_seen"]


def test_set_filter():
    s = Store(":memory:")
    job_id, _ = s.upsert(make_job())
    s.set_filter(job_id, Outcome("unknown", "remote, no region stated"))
    assert s.db.execute("SELECT filter_result FROM jobs").fetchone()[0] == "unknown"
