"""Video step: image + voiceover -> 9:16 MP4 wrapper, or (video material)
voiceover mixed over the source video."""

from __future__ import annotations

from typing import Any

from ..media.video import render_over_video, render_wrapper
from .base import StepContext, StepError, StepResult


class RenderVideoStep:
    name = "render_video"

    def cache_inputs(self, ctx: StepContext) -> dict[str, Any]:
        return {
            "media": [m.sha256 for m in ctx.state.media[:1]],
            "source_video": ctx.state.source_video.sha256 if ctx.state.source_video else None,
            "audio": ctx.state.artifacts.get("voice_audio"),
            "render": ctx.services.system.render.model_dump(),
            "wants_video": sorted(p for p, t in ctx.project.enabled_platforms().items() if t.wants_video),
        }

    async def run(self, ctx: StepContext) -> StepResult:
        source_video = ctx.state.source_video
        if not source_video and not any(t.wants_video for t in ctx.project.enabled_platforms().values()):
            return StepResult(skipped=True)
        audio = ctx.state.artifacts.get("voice_audio")
        if not audio:
            note = "озвучка не наложена: нет озвучки" if source_video else "видео не собрано: нет озвучки"
            return StepResult(skipped=True, warnings=[note])
        if not ctx.services.ffmpeg:
            return StepResult(skipped=True, warnings=["видео не собрано: FFmpeg не найден"])
        stem = audio.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        rel = f"video/{ctx.step.id}-{stem}.mp4"
        try:
            if source_video:
                await render_over_video(ctx.run_dir / source_video.path, ctx.run_dir / audio, ctx.run_dir / rel, ctx.services.system.render)
            elif ctx.state.media:
                await render_wrapper(ctx.run_dir / ctx.state.media[0].path, ctx.run_dir / audio, ctx.run_dir / rel, ctx.services.system.render)
            else:
                return StepResult(skipped=True, warnings=["видео не собрано: нет изображения"])
        except Exception as e:
            raise StepError(f"сборка видео не удалась: {e}") from e
        return StepResult(artifacts={"video": rel})
