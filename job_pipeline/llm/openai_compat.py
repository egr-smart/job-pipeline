"""Backend for any OpenAI-compatible chat API: OpenRouter, OpenAI, Ollama, LM Studio, vLLM, ..."""

import json
import os
import re
from urllib.parse import urlparse

import httpx
import openai

from ..config import OpenAICompatibleConfig
from .base import Completion, LLMError

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)  # reasoning models served without a parser


def parse_json_content(content: str) -> dict:
    text = _FENCE_RE.sub("", _THINK_RE.sub("", content).strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap the object in prose; take the outermost {...}.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"response is not JSON: {content[:300]}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"response is not JSON: {content[:300]}") from e
    if not isinstance(data, dict):
        raise LLMError(f"response is not a JSON object: {content[:300]}")
    return data


class OpenAICompatibleBackend:
    def __init__(self, cfg: OpenAICompatibleConfig, http_client: httpx.Client | None = None):
        self.cfg = cfg
        host = urlparse(cfg.base_url).hostname or "local"
        self.is_openrouter = host.endswith("openrouter.ai")
        self.id = f"{'openrouter' if self.is_openrouter else host}:{cfg.model}"
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        if cfg.api_key_env and not key:
            self._missing_key = f"set ${cfg.api_key_env} to use {cfg.base_url}"
        else:
            self._missing_key = None
        self.client = openai.OpenAI(
            api_key=key or "not-needed",  # local servers ignore it, but the SDK requires one
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            max_retries=2,  # SDK retries 429/5xx with backoff
            http_client=http_client,
            default_headers={"X-Title": "job-pipeline"} if self.is_openrouter else None,
        )

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        if self._missing_key:
            raise LLMError(self._missing_key, fatal=True)
        # The schema also goes in the prompt, so json_object mode (no schema support) still works.
        system = f"{system}\n\nRespond with only a JSON object matching this JSON Schema:\n{json.dumps(schema)}"
        if self.cfg.response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        else:
            response_format = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=response_format,
                temperature=self.cfg.temperature,
                # OpenRouter reports the real cost of each call when asked.
                extra_body={"usage": {"include": True}} if self.is_openrouter else None,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise LLMError(f"auth failed: {e}", fatal=True) from e
        except openai.NotFoundError as e:
            raise LLMError(f"model or endpoint not found: {e}", fatal=True) from e
        except openai.APIStatusError as e:
            # 402 = out of credits (OpenRouter); a 400 naming response_format means the model
            # can't do structured output, which every later call would hit too.
            fatal = e.status_code == 402 or (e.status_code == 400 and "response_format" in str(e))
            hint = " (try `response_format: json_object`)" if "response_format" in str(e) else ""
            raise LLMError(f"HTTP {e.status_code}: {e}{hint}", fatal=fatal) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise LLMError(f"cannot reach {self.cfg.base_url}: {e}") from e

        if not resp.choices or resp.choices[0].message.content is None:
            raise LLMError("empty response")
        usage = resp.usage
        cost = getattr(usage, "cost", None) if usage else None
        if cost is None and usage is not None and getattr(usage, "model_extra", None):
            cost = usage.model_extra.get("cost")
        return Completion(parse_json_content(resp.choices[0].message.content), cost)

