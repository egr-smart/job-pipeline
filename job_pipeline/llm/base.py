from dataclasses import dataclass
from typing import Protocol


class LLMError(Exception):
    """A call failed. `fatal` means retrying other jobs this run is pointless (auth, usage limit)."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


@dataclass
class Completion:
    data: dict
    cost_usd: float | None = None  # as reported by the backend; subscription CLIs report list-price equivalent


class Backend(Protocol):
    id: str  # identifies backend + model; part of the scores cache key

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        """One completion whose output is a JSON object matching `schema`."""
        ...
