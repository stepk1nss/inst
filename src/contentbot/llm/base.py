"""Provider-neutral LLM interface used by the steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

Role = Literal["generate", "classify", "judge"]


@dataclass
class ImagePart:
    path: Path


@dataclass
class TextPart:
    text: str


ContentPart = ImagePart | TextPart


@dataclass
class LLMCall:
    purpose: str
    role: Role
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Kept so tests (and the dev preview) can prove what a step actually received.
    system: str = ""
    text_input: str = ""
    images: list[str] = field(default_factory=list)


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str
    calls: list[LLMCall]

    async def generate_json(
        self, *, purpose: str, role: Role, system: str, content: list[ContentPart], schema: dict[str, Any]
    ) -> dict[str, Any]: ...


def text_of(content: list[ContentPart]) -> str:
    return "\n".join(p.text for p in content if isinstance(p, TextPart))


def images_of(content: list[ContentPart]) -> list[str]:
    return [str(p.path) for p in content if isinstance(p, ImagePart)]
