"""Instance configuration: config.yaml (what code acts on) + profile.md (what the LLM reads)."""

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocationConfig(_Strict):
    extra_allowed_regions: list[str] = []


class TitleConfig(_Strict):
    include: list[str] = Field(min_length=1)
    exclude: list[str] = []


class Limits(_Strict):
    max_age_days: int = 30
    max_jobs_per_run: int = 150
    active_window_days: int = 30


class ToggleSource(_Strict):
    enabled: bool = True


class CategorySource(ToggleSource):
    categories: list[str] = []


class HNSource(ToggleSource):
    threads: int = 2  # newest monthly threads to read; max_age_days drops older comments


class Sources(_Strict):
    watchlist: str | None = "watchlist.yaml"
    hn_who_is_hiring: HNSource = HNSource()
    remotive: ToggleSource = ToggleSource()
    weworkremotely: CategorySource = CategorySource(
        categories=["back-end-programming", "full-stack-programming", "devops-sysadmin"]
    )


class OpenAICompatibleConfig(_Strict):
    backend: Literal["openai_compatible"]
    base_url: str = "https://openrouter.ai/api/v1"
    model: str
    api_key_env: str | None = None  # env var holding the key; omit for local servers
    # json_schema = constrained structured output; json_object for models/servers without it.
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    temperature: float = 0.0
    timeout_seconds: float = 120


# Claude Code in headless mode, using the logged-in subscription. --system-prompt replaces
# Claude Code's own prompt and --tools "" disables tools, so each call is a plain completion.
CLAUDE_CLI_COMMAND = [
    "claude", "-p",
    "--model", "{model}",
    "--system-prompt", "{system_prompt}",
    "--json-schema", "{json_schema}",
    "--tools", "",
    "--output-format", "json",
    "--no-session-persistence",
]


class CliConfig(_Strict):
    """Any CLI that reads the user prompt on stdin and prints JSON.

    Placeholders in `command`: {model}, {system_prompt}, {json_schema}. Output may be the
    bare JSON object, or an envelope with `structured_output` or a JSON-string `result`.
    """

    backend: Literal["cli"]
    model: str = "sonnet"
    command: list[str] = CLAUDE_CLI_COMMAND
    timeout_seconds: float = 180


LLMBackendConfig = Annotated[OpenAICompatibleConfig | CliConfig, Field(discriminator="backend")]


class LLMExtractionOverride(_Strict):
    model: str | None = None


class LLMConfig(_Strict):
    scoring: LLMBackendConfig
    extraction: LLMExtractionOverride = LLMExtractionOverride()


class Weights(_Strict):
    role_fit: float = 0.5
    stack_fit: float = 0.3
    seniority: float = 0.2


class Ranking(_Strict):
    shortlist_size: int = 10
    # Points (0–10) per seniority verdict, scaled by weights.seniority. "under" is always dropped.
    seniority_scores: dict[Literal["stretch", "match", "over"], float] = {
        "match": 10.0, "over": 6.0, "stretch": 3.0,
    }
    unclear_list_size: int = 3
    weights: Weights = Weights()
    red_flag_penalty: float = 1.0
    min_score: float = 4.0  # ranked below this -> dropped, so weak fits never pad the shortlist
    resurface: bool = False


class FileNotifier(_Strict):
    type: Literal["file"]
    dir: str = "reports"


class EmailNotifier(_Strict):
    type: Literal["email"]
    smtp_host: str
    smtp_port: int = 587
    username_env: str
    password_env: str
    to: str
    from_: str | None = Field(default=None, alias="from")


class Config(_Strict):
    home_country: str
    state_dir: str = "."
    profile: str = "profile.md"
    evals_dir: str = "evals"
    location: LocationConfig = LocationConfig()
    titles: TitleConfig
    limits: Limits = Limits()
    sources: Sources = Sources()
    llm: LLMConfig | None = None
    ranking: Ranking = Ranking()
    notifiers: list[FileNotifier | EmailNotifier] = [FileNotifier(type="file")]

    # Directory the config file lives in; relative paths resolve against it.
    base_dir: Path = Path(".")

    @field_validator("home_country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def path(self, p: str) -> Path:
        return (self.base_dir / p).resolve()

    @property
    def db_path(self) -> Path:
        state = os.environ.get("JOBS_STATE_DIR") or self.state_dir
        return self.path(state) / "jobs.db"

    def read_profile(self) -> str:
        return self.path(self.profile).read_text()


class ConfigError(Exception):
    pass


def default_config_path() -> Path:
    return Path(os.environ.get("JOBS_CONFIG", "config.yaml"))


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (copy config.example.yaml to get started)")
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        return Config.model_validate({**raw, "base_dir": path.parent.resolve()})
    except ValidationError as e:
        raise ConfigError(f"invalid config {path}:\n{e}") from e
