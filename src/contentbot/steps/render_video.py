"""Video wrapper step: first image + voiceover -> 9:16 MP4."""

from __future__ import annotations

from typing import Any

from ..media.video import render_wrapper
from .base import StepContext, StepError, StepResult


class RenderVideoStep:
    name = "render_video"

    def cache_inputs(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "media": [m.sha256 for m in ctx.state.media[:1]],
            "audio": ctx.state.artifacts.get("voice_audio"),
            "render": ctx.services.system.render.model_dump(),
            "wants_video": sorted(p for p, t in ctx.project.enabled_platforms().items() if t.wants_video),
        }

    async def run(self, ctx: StepContext) -> StepResult:
        if not any(t.wants_video for t in ctx.project.enabled_platforms().values()):
            return StepResult(skipped=True)
        audio = ctx.state.artifacts.get("voice_audio")
        if not audio:
            return StepResult(skipped=True, warnings=["видео не собрано: нет озвучки"])
        if not ctx.services.ffmpeg:
            return StepResult(skipped=True, warnings=["видео не собрано: FFmpeg не найден"])
        if not ctx.state.media:
            return StepResult(skipped=True, warnings=["видео не собрано: нет изображения"])
        image = ctx.run_dir / ctx.state.media[0].path
        rel = f"video/{ctx.step.id}-{audio.rsplit('/', 1)[-1].rsplit('.', 1)[0]}.mp4"
        try:
            await render_wrapper(image, ctx.run_dir / audio, ctx.run_dir / rel, ctx.services.system.render)
        except Exception as e:
            raise StepError(f"сборка видео не удалась: {e}") from e
        return StepResult(artifacts={"video": rel})
