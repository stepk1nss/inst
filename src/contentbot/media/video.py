"""Simple video wrapper with FFmpeg: still image + voiceover -> vertical MP4.

The frame (blurred copy of the image as background + the full image centred)
is composed once with Pillow; FFmpeg only adds a slow zoom and the audio.
No AI video generation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from ..config.schema import RenderConfig


class VideoError(Exception):
    pass


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise VideoError(f"{args[0]} failed: {err.decode(errors='replace')[-500:]}")
    return out.decode()


async def media_duration(path: Path) -> float:
    out = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)
    )
    return float(out.strip())


def compose_frame(image: Path, out: Path, cfg: RenderConfig) -> Path:
    w, h = cfg.width, cfg.height
    with Image.open(image) as src:
        im = src.convert("RGB")
    # blur at 1/8 size and upscale: same look, a fraction of the cost
    small = ImageOps.fit(im, (max(1, w // 8), max(1, h // 8)), Image.Resampling.BILINEAR)
    bg = small.filter(ImageFilter.GaussianBlur(max(1, cfg.blur // 8))).resize((w, h), Image.Resampling.BICUBIC)
    fg = im.copy()
    fg.thumbnail((w, h), Image.Resampling.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, "JPEG", quality=92)
    return out


async def render_wrapper(image: Path, audio: Path, out: Path, cfg: RenderConfig) -> Path:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    duration = await media_duration(audio) + cfg.tail_seconds
    frame = compose_frame(image, out.with_suffix(".frame.jpg"), cfg)
    zoom = f"(1+{cfg.zoom - 1:.4f}*t/{duration:.3f})"
    vf = f"scale=w='trunc({w}*{zoom}/2)*2':h=-2:eval=frame,crop={w}:{h},setsar=1,format=yuv420p"
    try:
        await _run(
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-i", str(frame),
            "-i", str(audio),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(fps),
            "-c:a", "aac", "-b:a", "128k", "-af", f"apad=pad_dur={cfg.tail_seconds}",
            "-t", f"{duration:.3f}", "-movflags", "+faststart",
            str(out),
        )
    finally:
        frame.unlink(missing_ok=True)
    return out


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


async def has_audio(path: Path) -> bool:
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)
    )
    return bool(out.strip())


async def extract_frames(video: Path, out_dir: Path, count: int = 2) -> list[Path]:
    """Frames for analysis (the LLM gets images, not video): evenly spread, skipping the very start."""
    duration = await media_duration(video)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(count):
        t = duration * (0.15 + 0.7 * i / max(1, count - 1)) if count > 1 else duration * 0.3
        dst = out_dir / f"frame_{i}.jpg"
        await _run("ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(dst))
        frames.append(dst)
    return frames


async def render_over_video(video: Path, audio: Path, out: Path, cfg: RenderConfig, original_volume: float = 0.25) -> Path:
    """Source video fitted into 9:16 (blurred fill) + voiceover mixed over the original sound."""
    w, h, fps = cfg.width, cfg.height, cfg.fps
    vdur = await media_duration(video)
    adur = await media_duration(audio) + cfg.tail_seconds
    extend = max(0.0, adur - vdur)
    total = max(vdur, adur)
    video_graph = (
        f"[0:v]split[a][b];"
        f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},boxblur=10:2,scale={w}:{h}[bg];"
        f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,tpad=stop_mode=clone:stop_duration={extend:.3f},fps={fps},setsar=1,format=yuv420p[v]"
    )
    if await has_audio(video):
        audio_graph = (
            f"[0:a]volume={original_volume}[a0];[1:a]adelay=200:all=1[a1];"
            f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[aout]"
        )
    else:
        audio_graph = "[1:a]adelay=200:all=1[aout]"
    out.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
        "-filter_complex", f"{video_graph};{audio_graph}",
        "-map", "[v]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", "-movflags", "+faststart",
        str(out),
    )
    return out
