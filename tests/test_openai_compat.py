import json

import httpx
import pytest

from job_pipeline.config import OpenAICompatibleConfig
from job_pipeline.llm.base import LLMError
from job_pipeline.llm.openai_compat import OpenAICompatibleBackend, parse_json_content

GOOD = {"a": 1}


def _backend(handler, monkeypatch, **cfg) -> tuple[OpenAICompatibleBackend, list]:
    monkeypatch.setenv("TEST_KEY", "sk-test")
    seen = []

    def record(request: httpx.Request):
        seen.append(request)
        return handler(request)

    conf = OpenAICompatibleConfig(
        backend="openai_compatible", model="vendor/model", api_key_env="TEST_KEY",
        **{"base_url": "https://openrouter.ai/api/v1", **cfg},
    )
    backend = OpenAICompatibleBackend(conf, http_client=httpx.Client(transport=httpx.MockTransport(record)))
    backend.client = backend.client.with_options(max_retries=0)
    return backend, seen


def _chat(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "vendor/model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def test_request_shape_and_openrouter_cost(monkeypatch):
    backend, seen = _backend(
        lambda r: _chat(json.dumps(GOOD), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0012}),
        monkeypatch,
    )
    out = backend.complete_json("sys", "user", {"type": "object"})
    assert out.data == GOOD and out.cost_usd == 0.0012
    body = json.loads(seen[0].content)
    assert body["model"] == "vendor/model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert body["usage"] == {"include": True}
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert backend.id == "openrouter:vendor/model"


def test_local_server_gets_no_openrouter_extras(monkeypatch):
    backend, seen = _backend(lambda r: _chat(json.dumps(GOOD)), monkeypatch,
                             base_url="http://localhost:11434/v1", response_format="json_object")
    backend.complete_json("sys", "user", {})
    body = json.loads(seen[0].content)
    assert "usage" not in body and body["response_format"] == {"type": "json_object"}
    assert backend.id == "localhost:vendor/model"


@pytest.mark.parametrize("status, fatal", [(401, True), (402, True), (404, True), (500, False)])
def test_http_errors(monkeypatch, status, fatal):
    backend, _ = _backend(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}), monkeypatch)
    with pytest.raises(LLMError) as e:
        backend.complete_json("sys", "user", {})
    assert e.value.fatal is fatal


def test_missing_key_is_fatal(monkeypatch):
    conf = OpenAICompatibleConfig(backend="openai_compatible", model="m", api_key_env="DEFINITELY_UNSET_KEY")
    monkeypatch.delenv("DEFINITELY_UNSET_KEY", raising=False)
    with pytest.raises(LLMError) as e:
        OpenAICompatibleBackend(conf).complete_json("s", "u", {})
    assert e.value.fatal and "DEFINITELY_UNSET_KEY" in str(e.value)


@pytest.mark.parametrize("content", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '<think>hmm {not json}</think>\n{"a": 1}',
    'Sure! Here it is: {"a": 1} Hope that helps.',
])
def test_parse_json_content(content):
    assert parse_json_content(content) == GOOD


def test_parse_json_content_rejects_non_objects():
    with pytest.raises(LLMError):
        parse_json_content("[1, 2]")
