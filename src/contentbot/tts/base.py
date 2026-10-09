from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TTSError(Exception):
    pass


class TTS(Protocol):
    name: str
    calls: int

    async def synthesize(self, *, text: str, voice_id: str, speed: float, out_path: Path) -> Path: ...
