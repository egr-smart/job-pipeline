"""Golden-set evaluation: hand-labelled job snapshots scored by any model, compared to your labels.

Layout under `evals_dir`:
  labels.yaml               your verdicts, one entry per job
  jobs/<id>.json            snapshot of the listing as the scorer saw it
  results/<scorer>/<hash>.json   cached model output per (model, prompt+profile)
"""

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from .config import Config
from .llm import LLMError
from .llm.base import Backend
from .llm.scoring import ScoreResult, job_message, prompt_hash, score_job, system_prompt
from .models import RawJob
from .rank import drop_reason, rank_score
from .store import Store

Verdict = Literal["strong", "maybe", "no"]
Location = Literal["yes", "no", "unclear"]
VERDICTS: list[Verdict] = ["strong", "maybe", "no"]


class Label(BaseModel):
    id: int
    company: str
    title: str
    verdict: Verdict
    location: Location | None = None  # only compared when you set it
    note: str | None = None


class Snapshot(BaseModel):
    job: RawJob
    filter_result: str
    filter_reason: str


class EvalSet:
    def __init__(self, root: Path):
        self.root = root
        self.labels_path = root / "labels.yaml"

    def labels(self) -> list[Label]:
        if not self.labels_path.exists():
            return []
        return [Label.model_validate(x) for x in yaml.safe_load(self.labels_path.read_text()) or []]

    def save_labels(self, labels: list[Label]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = [l.model_dump(exclude_none=True) for l in sorted(labels, key=lambda l: l.id)]
        self.labels_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))

    def snapshot_path(self, job_id: int) -> Path:
        return self.root / "jobs" / f"{job_id}.json"

    def add(self, store: Store, job_id: int, verdict: Verdict, location: Location | None, note: str | None) -> Label:
        row = store.db.execute(
            "SELECT filter_result, filter_reason FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = store.get_job(job_id)
        snap = Snapshot(job=job, filter_result=row["filter_result"] or "unknown", filter_reason=row["filter_reason"] or "")
        path = self.snapshot_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snap.model_dump_json(indent=1))
        label = Label(id=job_id, company=job.company, title=job.title, verdict=verdict, location=location, note=note)
        self.save_labels([l for l in self.labels() if l.id != job_id] + [label])
        return label

    def snapshot(self, job_id: int) -> Snapshot:
        return Snapshot.model_validate_json(self.snapshot_path(job_id).read_text())

    def results_path(self, scorer_id: str, phash: str) -> Path:
        return self.root / "results" / re.sub(r"[^A-Za-z0-9._-]+", "_", scorer_id) / f"{phash}.json"


@dataclass
class Case:
    label: Label
    result: ScoreResult | None = None
    error: str | None = None
    score: float | None = None
    dropped: str | None = None
    predicted: Verdict | None = None


@dataclass
class EvalReport:
    scorer_id: str
    cases: list[Case] = field(default_factory=list)
    cached: int = 0
    cost_usd: float = 0.0
    aborted: str | None = None

    @property
    def scored(self) -> list[Case]:
        return [c for c in self.cases if c.predicted]

    @property
    def verdict_agreement(self) -> tuple[int, int]:
        return sum(c.predicted == c.label.verdict for c in self.scored), len(self.scored)

    @property
    def location_agreement(self) -> tuple[int, int]:
        with_loc = [c for c in self.scored if c.label.location]
        return sum(c.result.location_eligible == c.label.location for c in with_loc), len(with_loc)

    @property
    def serious_misses(self) -> list[Case]:
        """You said strong and the model dropped it, or the reverse: the errors that cost you."""
        return [c for c in self.scored if {c.label.verdict, c.predicted} == {"strong", "no"}]

    def confusion(self) -> Counter:
        return Counter((c.label.verdict, c.predicted) for c in self.scored)


def predict(result: ScoreResult, cfg: Config, strong_at: float) -> tuple[Verdict, float, str | None]:
    """Map a score onto the label scale the way the digest would treat it."""
    score = rank_score(result, cfg.ranking)
    dropped = drop_reason(result) or (
        f"score {score} below min_score {cfg.ranking.min_score}" if score < cfg.ranking.min_score else None
    )
    if dropped:
        return "no", score, dropped
    return ("strong" if score >= strong_at else "maybe"), score, None


def run_eval(
    cfg: Config,
    evals: EvalSet,
    backend: Backend,
    *,
    strong_at: float,
    progress: Callable[[str], None] = lambda _: None,
) -> EvalReport:
    system = system_prompt(cfg.read_profile(), cfg.home_country)
    cache_path = evals.results_path(backend.id, prompt_hash(system))
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    report = EvalReport(backend.id)
    labels = evals.labels()

    for n, label in enumerate(labels, 1):
        case = Case(label)
        report.cases.append(case)
        key = str(label.id)
        if key in cache:
            case.result = ScoreResult.model_validate(cache[key])
            report.cached += 1
        elif report.aborted:
            case.error = "skipped (scoring aborted)"
            continue
        else:
            snap = evals.snapshot(label.id)
            try:
                case.result, cost = score_job(
                    backend, system, job_message(snap.job, snap.filter_result, snap.filter_reason)
                )
            except LLMError as e:
                case.error = str(e)
                progress(f"[{n}/{len(labels)}] #{label.id} FAILED: {e}")
                if e.fatal:
                    report.aborted = str(e)
                continue
            report.cost_usd += cost or 0
            cache[key] = case.result.model_dump()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1))  # save as we go; runs can be slow
            progress(f"[{n}/{len(labels)}] #{label.id} {label.company} — {label.title[:60]}")
        case.predicted, case.score, case.dropped = predict(case.result, cfg, strong_at)
    return report


def format_report(r: EvalReport, *, verbose: bool = True) -> str:
    agree, total = r.verdict_agreement
    loc_agree, loc_total = r.location_agreement
    pct = lambda a, b: f"{a}/{b} ({100 * a // b}%)" if b else "n/a"
    lines = [
        f"Model {r.scorer_id}: {len(r.scored)}/{len(r.cases)} jobs scored ({r.cached} cached)"
        + (f", ≈${r.cost_usd:.3f}" if r.cost_usd else ""),
        f"  Verdict agreement:  {pct(agree, total)}   serious misses (strong↔no): {len(r.serious_misses)}",
        f"  Location agreement: {pct(loc_agree, loc_total)}",
    ]
    if r.aborted:
        lines.append(f"  Aborted: {r.aborted}")
    errors = [c for c in r.cases if c.error]
    if errors:
        lines.append(f"  Errors: {len(errors)}")
    if not verbose:
        return "\n".join(lines)

    conf = r.confusion()
    lines += ["", "  Confusion (rows = you, columns = model):", "            " + "".join(f"{v:>8}" for v in VERDICTS)]
    for mine in VERDICTS:
        lines.append(f"  {mine:>8}  " + "".join(f"{conf[(mine, theirs)]:>8}" for theirs in VERDICTS))

    wrong = sorted(
        [c for c in r.scored if c.predicted != c.label.verdict or (c.label.location and c.result.location_eligible != c.label.location)],
        key=lambda c: c not in r.serious_misses,
    )
    if wrong:
        lines += ["", "  Disagreements (serious first):"]
    for c in wrong:
        l, res = c.label, c.result
        what = []
        if c.predicted != l.verdict:
            what.append(f"you {l.verdict} → model {c.predicted} (score {c.score}{'; ' + c.dropped if c.dropped else ''})")
        if l.location and res.location_eligible != l.location:
            what.append(f"location: you {l.location} → model {res.location_eligible}")
        lines.append(f"  #{l.id} {l.company} — {l.title[:60]}")
        lines.append(f"      {' | '.join(what)}")
        if l.note:
            lines.append(f"      your note: {l.note}")
        lines.append(f"      model: {res.summary}")
    return "\n".join(lines)
