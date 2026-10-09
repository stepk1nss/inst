"""ElevenLabs text-to-speech over REST."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from .base import TTSError

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(self, model: str, api_key: str | None = None, timeout: float = 60.0):
        self.model = model
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.timeout = timeout
        self.calls = 0

    async def synthesize(self, *, text: str, voice_id: str, speed: float, out_path: Path) -> Path:
        if not voice_id:
            raise TTSError("не задан voice ID: укажите голос проекта или ELEVENLABS_FALLBACK_VOICE_ID")
        self.calls += 1
        payload = {"text": text, "model_id": self.model, "voice_settings": {"speed": speed}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                API.format(voice_id=voice_id),
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
                json=payload,
            )
        if resp.status_code != 200:
            raise TTSError(f"ElevenLabs вернул {resp.status_code}: {resp.text[:300]}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path
