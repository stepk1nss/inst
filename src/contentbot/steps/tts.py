"""Voiceover step. Depends only on the text and the voice, so a voice change
re-runs this step (and the video) without touching the LLM steps."""

from __future__ import annotations

import hashlib
from typing import Any

from ..config.schema import Project
from ..core.state import RunState
from .base import StepContext, StepError, StepResult


def resolve_voice(project: Project, state: RunState, fallback_voice_id: str) -> tuple[str, str, float]:
    """-> (voice option id, provider voice id, speed). Post params override project defaults."""
    voice_key = state.params.get("voice_id") or project.voice.default or ""
    option = project.voice.get(voice_key)
    provider_id = state.params.get("voice_provider_id") or (option.provider_voice_id if option else voice_key)
    if not provider_id:
        provider_id = fallback_voice_id
    speed = float(state.params.get("speed") or project.voice.speed)
    return voice_key, provider_id, speed


class TTSStep:
    name = "tts"

    def cache_inputs(self, ctx: StepContext) -> dict[str, Any]:
        voice_key, provider_id, speed = resolve_voice(ctx.project, ctx.state, ctx.services.system.tts.fallback_voice_id)
        tts = ctx.services.tts
        return {
            "text": ctx.state.fields.get(ctx.step.input or ""),
            "voice": voice_key,
            "provider_voice": provider_id,
            "speed": speed,
            "provider": getattr(tts, "name", None),
            "model": ctx.services.system.tts.model,
            "enabled": ctx.project.voice.enabled,
        }

    async def run(self, ctx: StepContext) -> StepResult:
        if not ctx.project.voice.enabled:
            return StepResult(skipped=True, warnings=["озвучка выключена в настройках проекта"])
        if ctx.services.tts is None:
            return StepResult(skipped=True, warnings=["озвучка не подключена (нет TTS-провайдера)"])
        text = ctx.state.fields.get(ctx.step.input or "")
        if not isinstance(text, str) or not text.strip():
            return StepResult(skipped=True, warnings=[f"поле '{ctx.step.input}' пустое — озвучка пропущена"])

        voice_key, provider_id, speed = resolve_voice(ctx.project, ctx.state, ctx.services.system.tts.fallback_voice_id)
        tag = hashlib.sha1(f"{voice_key}|{provider_id}|{speed}|{text}".encode()).hexdigest()[:10]
        rel = f"audio/{ctx.step.id}-{tag}.mp3"
        try:
            await ctx.services.tts.synthesize(text=text, voice_id=provider_id, speed=speed, out_path=ctx.run_dir / rel)
        except Exception as e:  # provider errors become a readable step error
            raise StepError(f"озвучка не удалась: {e}") from e
        warnings = []
        if ctx.services.tts.name == "mock":
            warnings.append("озвучка-заглушка (dev): тон вместо голоса")
        elif not ctx.project.voice.get(voice_key) or not ctx.project.voice.get(voice_key).provider_voice_id:
            warnings.append("использован временный голос")
        return StepResult(artifacts={"voice_audio": rel}, warnings=warnings)
