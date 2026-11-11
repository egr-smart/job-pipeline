import pytest

from job_pipeline.evals import EvalSet, format_report, predict, run_eval
from job_pipeline.llm.base import Completion, LLMError
from job_pipeline.llm.scoring import ScoreResult
from job_pipeline.models import Outcome
from job_pipeline.store import Store

from .conftest import make_job

BASE = dict(summary="s", gaps=[], red_flags=[], location_eligible="yes", location_note="n",
            salary_text=None, role_fit=8, stack_fit=8, seniority="match")


class FakeBackend:
    id = "fake:m"

    def __init__(self, by_title: dict):
        self.by_title, self.calls = by_title, 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        for title, out in self.by_title.items():
            if f"Title: {title}" in user:
                if isinstance(out, Exception):
                    raise out
                return Completion({**BASE, **out}, 0.001)
        raise AssertionError(user)


@pytest.fixture
def evalset(tmp_path, cfg):
    cfg.base_dir = tmp_path
    (tmp_path / "profile.md").write_text("I like platforms.")
    store = Store(":memory:")
    ev = EvalSet(tmp_path / "evals")
    for sid, title, verdict, loc in [("1", "Platform A", "strong", "yes"), ("2", "Platform B", "no", "no"),
                                     ("3", "Platform C", "maybe", None)]:
        job_id, _ = store.upsert(make_job(source_id=sid, title=title))
        store.set_filter(job_id, Outcome("allow", "ok"))
        ev.add(store, job_id, verdict, loc, None)
    return ev


def test_add_snapshots_and_relabels(evalset, tmp_path):
    assert [l.verdict for l in evalset.labels()] == ["strong", "no", "maybe"]
    assert evalset.snapshot(1).job.title == "Platform A"
    store = Store(":memory:")
    store.upsert(make_job(source_id="1", title="Platform A"))
    evalset.add(store, 1, "maybe", None, "changed my mind")
    assert [(l.id, l.verdict) for l in evalset.labels()][0] == (1, "maybe")


@pytest.mark.parametrize("overrides, expected", [
    ({}, "strong"),
    ({"role_fit": 4, "stack_fit": 4}, "maybe"),   # score 5.2: kept, below strong_at
    ({"seniority": "under"}, "no"),
    ({"location_eligible": "no"}, "no"),
    ({"role_fit": 2, "stack_fit": 2}, "no"),      # below min_score
])
def test_predict(cfg, overrides, expected):
    verdict, _, _ = predict(ScoreResult.model_validate({**BASE, **overrides}), cfg, strong_at=6.0)
    assert verdict == expected


def test_run_eval_scores_compares_and_caches(evalset, cfg):
    backend = FakeBackend({
        "Platform A": {},                                     # strong, location yes: agree
        "Platform B": {"location_eligible": "yes"},           # you said no/no: serious miss
        "Platform C": {"role_fit": 4, "stack_fit": 4},        # maybe: agree
    })
    report = run_eval(cfg, evalset, backend, strong_at=6.0)
    assert report.verdict_agreement == (2, 3)
    assert report.location_agreement == (1, 2)
    assert [c.label.id for c in report.serious_misses] == [2]
    text = format_report(report)
    assert "#2 Acme — Platform B" in text and "you no → model strong" in text

    again = run_eval(cfg, evalset, backend, strong_at=6.0)
    assert backend.calls == 3 and again.cached == 3


def test_fatal_error_stops_further_calls(evalset, cfg):
    backend = FakeBackend({"Platform A": LLMError("auth failed", fatal=True), "Platform B": {}, "Platform C": {}})
    report = run_eval(cfg, evalset, backend, strong_at=6.0)
    assert backend.calls == 1 and report.aborted and len(report.scored) == 0
