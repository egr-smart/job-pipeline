import json
import sys
import textwrap

import pytest

from job_pipeline.config import CliConfig, Ranking
from job_pipeline.llm.base import Completion, LLMError
from job_pipeline.llm.cli import CliBackend
from job_pipeline.llm.scoring import ScoreResult, job_message, prompt_hash, score_job, system_prompt
from job_pipeline.models import Outcome
from job_pipeline.rank import drop_reason, rank_score
from job_pipeline.store import Store

from .conftest import make_job

GOOD = dict(
    summary="Fits.", gaps=["Go"], red_flags=[], location_eligible="yes", location_note="APAC",
    salary_text=None, role_fit=8, stack_fit=6, seniority="match",
)


class FakeBackend:
    id = "fake:model"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        return Completion(self.outputs.pop(0), 0.01)


def test_score_job_retries_once_on_invalid_output():
    backend = FakeBackend({"nope": 1}, GOOD)
    result, cost = score_job(backend, "sys", "msg")
    assert result.role_fit == 8 and backend.calls == 2 and cost == pytest.approx(0.02)


def test_score_job_gives_up_after_retry():
    with pytest.raises(LLMError):
        score_job(FakeBackend({"nope": 1}, {"nope": 2}), "sys", "msg")


def test_long_lists_are_trimmed():
    assert len(ScoreResult.model_validate({**GOOD, "gaps": list("abcdefg")}).gaps) == 5


def test_prompt_hash_tracks_profile_changes():
    assert prompt_hash(system_prompt("A", "AU")) != prompt_hash(system_prompt("B", "AU"))
    assert "Candidate profile" in system_prompt("A", "AU")
    assert "{profile}" not in system_prompt("A", "AU")


def test_job_message_includes_filter_verdict_and_truncates():
    msg = job_message(make_job(description_text="x" * 20_000), "unknown", "remote, no region stated")
    assert "Pre-filter location verdict: unknown" in msg and msg.endswith("[truncated]")


@pytest.mark.parametrize(
    "overrides, dropped",
    [({}, None), ({"location_eligible": "no"}, "location"), ({"seniority": "under"}, "seniority")],
)
def test_ranking(overrides, dropped):
    r = ScoreResult.model_validate({**GOOD, **overrides})
    reason = drop_reason(r)
    assert (reason is None) if dropped is None else reason.startswith(dropped)


def test_stretch_kept_but_ranked_below_match():
    match = ScoreResult.model_validate(GOOD)
    stretch = ScoreResult.model_validate({**GOOD, "seniority": "stretch"})
    assert drop_reason(stretch) is None
    assert rank_score(stretch, Ranking()) < rank_score(match, Ranking())


def test_red_flags_lower_rank():
    clean = ScoreResult.model_validate(GOOD)
    flagged = ScoreResult.model_validate({**GOOD, "red_flags": ["on-call"]})
    assert rank_score(flagged, Ranking()) < rank_score(clean, Ranking())


def _fake_cli(tmp_path, envelope: dict, code: int = 0) -> CliBackend:
    script = tmp_path / "fake_cli.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys
        sys.stdin.read()
        print(json.dumps({envelope!r}))
        sys.exit({code})
    """))
    return CliBackend(CliConfig(backend="cli", command=[sys.executable, str(script), "{model}", "{system_prompt}"]))


def test_cli_backend_reads_structured_output(tmp_path):
    backend = _fake_cli(tmp_path, {"structured_output": GOOD, "total_cost_usd": 0.02, "is_error": False})
    out = backend.complete_json("sys", "user", {})
    assert out.data == GOOD and out.cost_usd == 0.02


def test_cli_backend_reads_json_string_result(tmp_path):
    out = _fake_cli(tmp_path, {"result": json.dumps(GOOD)}).complete_json("sys", "user", {})
    assert out.data == GOOD


def test_cli_backend_usage_limit_is_fatal(tmp_path):
    backend = _fake_cli(tmp_path, {"is_error": True, "result": "Claude usage limit reached"}, code=1)
    with pytest.raises(LLMError) as e:
        backend.complete_json("sys", "user", {})
    assert e.value.fatal


def test_jobs_to_score_skips_scored_marked_and_unseen():
    s = Store(":memory:")
    run = "2026-11-11T00:00:00+00:00"
    ids = [s.upsert(make_job(source_id=str(i), title=f"Platform Engineer {i}"), run)[0] for i in range(4)]
    stale, _ = s.upsert(make_job(source_id="old", title="Old Platform Engineer"), "2026-10-01T00:00:00+00:00")
    for i in ids + [stale]:
        s.set_filter(i, Outcome("allow", "ok"))
    s.save_score(ids[0], "h", "fake", "{}")
    s.db.execute("INSERT INTO marks VALUES (?, 'dismissed', NULL, ?)", (ids[1], run))
    assert sorted(s.jobs_to_score(run, "h", "fake", resurface=False)) == sorted(ids[2:])
    assert ids[0] in s.jobs_to_score(run, "other-profile", "fake", resurface=False)
