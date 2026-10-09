"""Offline TTS for dev mode: a quiet tone whose length follows the text length.

Lets the whole pipeline (audio -> video -> preview) run without an API key.
Pitch depends on the voice id, so a voice change is audible in the preview.
"""

from __future__ import annotations

import asyncio
import zlib
from pathlib import Path

from .base import TTSError

CHARS_PER_SECOND = 15.0


def mock_duration(text: str, speed: float) -> float:
    return max(2.0, min(60.0, len(text) / CHARS_PER_SECOND / max(speed, 0.1)))


class MockTTS:
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, *, text: str, voice_id: str, speed: float, out_path: Path) -> Path:
        self.calls += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        freq = 220 + zlib.crc32(voice_id.encode()) % 440
        dur = mock_duration(text, speed)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur:.2f}",
            "-af", "volume=0.15", "-c:a", "libmp3lame", "-b:a", "96k", str(out_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise TTSError(f"mock TTS: ffmpeg failed: {err.decode(errors='replace')[:300]}")
        return out_path
