import hashlib
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..models import RawJob
from .base import Backend, LLMError

MAX_DESCRIPTION_CHARS = 12_000


class ScoreResult(BaseModel):
    # Field order matters: reasoning fields come before the numbers they justify.
    model_config = ConfigDict(extra="forbid")

    summary: str
    gaps: list[str]
    red_flags: list[str]
    location_eligible: Literal["yes", "no", "unclear"]
    location_note: str
    salary_text: str | None
    role_fit: int = Field(ge=0, le=10)
    stack_fit: int = Field(ge=0, le=10)
    seniority: Literal["under", "stretch", "match", "over"]

    @field_validator("gaps", "red_flags")
    @classmethod
    def _at_most_five(cls, v: list[str]) -> list[str]:
        return v[:5]  # the prompt asks for <= 5; trim rather than fail the whole score


SCHEMA = ScoreResult.model_json_schema()


def system_prompt(profile: str, home_country: str) -> str:
    template = resources.files("job_pipeline.llm.prompts").joinpath("score.md").read_text()
    return template.replace("{profile}", profile.strip()).replace("{home_country}", home_country)


def prompt_hash(system: str) -> str:
    """Cache key for scores: changes when the profile, home country or prompt changes."""
    return hashlib.sha256(system.encode()).hexdigest()[:12]


def job_message(job: RawJob, filter_result: str, filter_reason: str) -> str:
    desc = job.description_text
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[:MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    lines = [
        f"Company: {job.company}",
        f"Title: {job.title}",
        f"Location (as listed): {job.location_raw or '(none)'}",
        f"Workplace type (structured, if provided): {job.workplace or '(not provided)'}",
        f"Pre-filter location verdict: {filter_result} ({filter_reason})",
    ]
    if job.salary_raw:
        lines.append(f"Salary (structured field): {job.salary_raw}")
    return "\n".join(lines) + f"\n\nDescription:\n{desc}"


def score_job(backend: Backend, system: str, message: str) -> tuple[ScoreResult, float | None]:
    """Score one job. Retries once if the output doesn't validate."""
    cost = 0.0
    for attempt in range(2):
        completion = backend.complete_json(system, message, SCHEMA)
        cost += completion.cost_usd or 0
        try:
            return ScoreResult.model_validate(completion.data), cost or None
        except ValidationError as e:
            if attempt:
                raise LLMError(f"invalid ScoreResult after retry: {e}") from e
    raise AssertionError("unreachable")
