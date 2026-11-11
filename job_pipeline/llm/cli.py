"""Backend that shells out to a CLI (default: Claude Code headless on your subscription)."""

import json
import re
import subprocess
import tempfile

from ..config import CliConfig
from .base import Completion, LLMError

_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|quota|not logged in|authenticat|/login", re.I)


class CliBackend:
    def __init__(self, cfg: CliConfig):
        self.cfg = cfg
        self.id = f"cli:{cfg.model}"
        # Run outside the repo so the CLI doesn't pick up project files such as CLAUDE.md.
        self._cwd = tempfile.mkdtemp(prefix="job-pipeline-llm-")

    def _argv(self, system: str, schema: dict) -> list[str]:
        subs = {"{model}": self.cfg.model, "{system_prompt}": system, "{json_schema}": json.dumps(schema)}
        argv = []
        for arg in self.cfg.command:
            for key, value in subs.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        return argv

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        try:
            proc = subprocess.run(
                self._argv(system, schema), input=user, capture_output=True, text=True,
                timeout=self.cfg.timeout_seconds, cwd=self._cwd,
            )
        except FileNotFoundError as e:
            raise LLMError(f"command not found: {self.cfg.command[0]}", fatal=True) from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"timed out after {self.cfg.timeout_seconds}s") from e

        out = proc.stdout.strip()
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            envelope = None

        if proc.returncode != 0 or (isinstance(envelope, dict) and envelope.get("is_error")):
            detail = (envelope or {}).get("result") if isinstance(envelope, dict) else None
            detail = str(detail or proc.stderr.strip() or out)[:500]
            raise LLMError(f"exit {proc.returncode}: {detail}", fatal=bool(_LIMIT_RE.search(detail)))
        if not isinstance(envelope, dict):
            raise LLMError(f"output is not a JSON object: {out[:300]}")

        cost = envelope.get("total_cost_usd")
        if isinstance(envelope.get("structured_output"), dict):
            return Completion(envelope["structured_output"], cost)
        if isinstance(envelope.get("result"), str):
            try:
                return Completion(json.loads(envelope["result"]), cost)
            except json.JSONDecodeError as e:
                raise LLMError(f"result is not JSON: {envelope['result'][:300]}") from e
        return Completion(envelope)
