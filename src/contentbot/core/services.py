"""External services bundle, chosen by settings (real or mock)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config.schema import SystemConfig
from ..llm.base import LLM
from ..settings import Settings
from ..tts.base import TTS


@dataclass
class Services:
    llm: LLM
    tts: TTS | None
    system: SystemConfig
    ffmpeg: bool
    dev: bool


def load_mock_fixtures(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: mock fixtures must be a mapping")
    return data


def build_services(settings: Settings, system: SystemConfig, mock_fixtures: dict[str, Any] | None = None) -> Services:
    if settings.llm_provider == "anthropic":
        from ..llm.claude import ClaudeLLM

        llm: LLM = ClaudeLLM(system.models)
    else:
        from ..llm.mock import MockLLM

        llm = MockLLM(mock_fixtures)

    tts: TTS | None
    if settings.tts_provider == "elevenlabs":
        from ..tts.elevenlabs import ElevenLabsTTS

        tts = ElevenLabsTTS(system.tts.model)
    elif settings.tts_provider == "mock" and settings.ffmpeg:
        from ..tts.mock import MockTTS

        tts = MockTTS()
    else:
        tts = None
    return Services(llm=llm, tts=tts, system=system, ffmpeg=settings.ffmpeg, dev=settings.dev)
