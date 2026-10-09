"""ClaudeLLM request shape, checked with a stub client (no network, no key)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from contentbot.llm.base import ImagePart, LLMError, TextPart
from contentbot.llm.claude import ClaudeLLM


class _Endpoint:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        model="claude-x",
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def _client(resp):
    std, beta = _Endpoint(resp), _Endpoint(resp)
    return SimpleNamespace(messages=std, beta=SimpleNamespace(messages=beta)), std, beta


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}


def test_generate_uses_structured_output_and_fallback(registry, tmp_path, demo_image):
    client, std, beta = _client(_response({"a": "ok"}))
    llm = ClaudeLLM(registry.system.models, client=client)
    out = asyncio.run(llm.generate_json(purpose="p", role="generate", system="sys",
                                        content=[ImagePart(demo_image), TextPart("hi")], schema=SCHEMA))
    assert out == {"a": "ok"}
    kw = beta.kwargs  # the generate model accepts server-side refusal fallback
    assert kw["model"] == registry.system.models.generate.model
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    blocks = kw["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["type"] == "base64"
    assert blocks[1] == {"type": "text", "text": "hi"}
    assert std.kwargs is None


def test_classify_model_uses_plain_endpoint(registry):
    client, std, beta = _client(_response({"a": "ok"}))
    llm = ClaudeLLM(registry.system.models, client=client)
    asyncio.run(llm.generate_json(purpose="p", role="classify", system="s", content=[TextPart("x")], schema=SCHEMA))
    assert std.kwargs["model"] == registry.system.models.classify.model
    assert beta.kwargs is None


def test_refusal_is_an_error(registry):
    client, _, _ = _client(_response({}, stop_reason="refusal"))
    llm = ClaudeLLM(registry.system.models, client=client)
    with pytest.raises(LLMError):
        asyncio.run(llm.generate_json(purpose="p", role="classify", system="s", content=[TextPart("x")], schema=SCHEMA))
