"""Deterministic offline LLM for dev mode and tests.

Answers are built from the JSON schema, optionally overridden per purpose by a
fixtures mapping (e.g. loaded from a *.mock.yaml file):

    teaser: {teaser: "...", voiceover: "..."}   # matched by step id
    my_scenario.solve: {...}                    # or by "<scenario>.<step>"
    classify: {scenario: my_scenario}
    judge: {leak: false, reason: ""}
"""

from __future__ import annotations

import copy
from typing import Any

from .base import ContentPart, LLMCall, Role, images_of, text_of


def _fake(schema: dict[str, Any], label: str) -> Any:
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: _fake(v, f"{label}.{k}") for k, v in schema.get("properties", {}).items()}
    if t == "array":
        if label.endswith("hashtags"):
            return ["#mock1", "#mock2"]
        return [_fake(schema.get("items", {"type": "string"}), f"{label}[{i}]") for i in range(2)]
    if t == "boolean":
        return False
    if t == "integer":
        return 1
    if t == "number":
        return 0.5
    return f"[mock {label}]"


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)


class MockLLM:
    name = "mock"

    def __init__(self, fixtures: dict[str, Any] | None = None):
        self.fixtures = fixtures or {}
        self.calls: list[LLMCall] = []

    async def generate_json(
        self, *, purpose: str, role: Role, system: str, content: list[ContentPart], schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(
            LLMCall(
                purpose=purpose,
                role=role,
                model="mock",
                system=system,
                text_input=text_of(content),
                images=images_of(content),
            )
        )
        result = _fake(schema, purpose.split(".")[-1])
        short = purpose.split(".")[-1]
        for key in (short, purpose):
            if key in self.fixtures:
                result = _merge(result, self.fixtures[key])
        # keep only keys allowed by the schema (fixtures may be broader)
        props = schema.get("properties")
        if isinstance(result, dict) and props is not None:
            result = {k: v for k, v in result.items() if k in props}
        return result
