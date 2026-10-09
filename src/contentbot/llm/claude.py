"""Anthropic Claude provider: vision input + structured JSON output."""

from __future__ import annotations

import base64
import json
from typing import Any

import anthropic

from ..config.schema import ModelsConfig
from .base import ContentPart, ImagePart, LLMCall, LLMError, Role, TextPart, images_of, text_of

# Models that accept the server-side refusal fallback (routes a declined request
# to another model inside the same call).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _media_type(path: str) -> str:
    return "image/png" if path.lower().endswith(".png") else "image/jpeg"


class ClaudeLLM:
    name = "anthropic"

    def __init__(self, models: ModelsConfig, client: anthropic.AsyncAnthropic | None = None):
        self.models = models
        self.client = client or anthropic.AsyncAnthropic()
        self.calls: list[LLMCall] = []

    def _blocks(self, content: list[ContentPart]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, ImagePart):
                data = base64.standard_b64encode(part.path.read_bytes()).decode("ascii")
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": _media_type(str(part.path)), "data": data}}
                )
            elif isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
        return blocks

    async def generate_json(
        self, *, purpose: str, role: Role, system: str, content: list[ContentPart], schema: dict[str, Any]
    ) -> dict[str, Any]:
        spec = getattr(self.models, role)
        kwargs: dict[str, Any] = dict(
            model=spec.model,
            max_tokens=spec.max_tokens,
            system=system,
            messages=[{"role": "user", "content": self._blocks(content)}],
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": spec.effort},
        )
        try:
            if spec.model in _FALLBACK_MODELS:
                resp = await self.client.beta.messages.create(**kwargs, betas=[_FALLBACK_BETA], fallbacks="default")
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(f"{purpose}: rate limited by Anthropic, retry later") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"{purpose}: Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{purpose}: cannot reach Anthropic API") from e

        self.calls.append(
            LLMCall(
                purpose=purpose,
                role=role,
                model=getattr(resp, "model", spec.model),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                system=system,
                text_input=text_of(content),
                images=images_of(content),
            )
        )
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            reason = getattr(details, "explanation", None) or "no details"
            raise LLMError(f"{purpose}: the model declined the request ({reason})")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: response was cut off (max_tokens)")
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"{purpose}: model returned invalid JSON") from e
