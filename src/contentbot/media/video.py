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
