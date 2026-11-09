from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Workplace = Literal["remote", "hybrid", "onsite"]


class RawJob(BaseModel):
    """A listing as fetched from a source, normalised to a common shape."""

    source: str
    source_id: str
    url: str
    company: str
    title: str
    location_raw: str = ""
    description_text: str = ""
    posted_at: datetime | None = None
    salary_raw: str | None = None
    # Structured workplace type when the source provides one. Remote-only boards set "remote".
    workplace: Workplace | None = None


FilterResult = Literal["allow", "deny", "unknown"]


@dataclass(frozen=True)
class Outcome:
    """Result of the hard filters. "unknown" means location eligibility is left to the LLM."""

    result: FilterResult
    reason: str
