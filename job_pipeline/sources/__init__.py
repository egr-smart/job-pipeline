"""Job sources. Each adapter turns a remote feed into a list of RawJob."""

from dataclasses import dataclass, field

from ..models import RawJob


@dataclass
class SourceResult:
    """Outcome of fetching one source (or one watchlist board)."""

    name: str
    jobs: list[RawJob] = field(default_factory=list)
    error: str | None = None
