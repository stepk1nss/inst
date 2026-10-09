"""Audio helpers: Telegram voice messages must be OGG/Opus."""

from __future__ import annotations

from pathlib import Path

from .video import _run


async def to_ogg_opus(src: Path, dst: Path | None = None) -> Path:
    dst = dst or src.with_suffix(".ogg")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:a", "libopus", "-b:a", "48k", "-ac", "1", str(dst))
    return dst
