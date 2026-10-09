"""Publisher interface + dry-run publisher used in dev mode.

Real adapters (Telegram, Instagram, TikTok, YouTube) implement the same
interface in M3/M4. In dev mode publishing is ALWAYS a dry run: the would-be
request is written to data/runs/<id>/outbox/<platform>.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..core.state import PlatformPost


@dataclass
class PublishResult:
    platform: str
    ok: bool
    status: str  # published | draft | private | dry_run | failed
    url: str | None = None
    external_id: str | None = None
    error: str | None = None


class Publisher(Protocol):
    platform: str

    async def publish(self, post: PlatformPost, run_dir: Path) -> PublishResult: ...


class DryRunPublisher:
    def __init__(self, platform: str):
        self.platform = platform

    async def publish(self, post: PlatformPost, run_dir: Path) -> PublishResult:
        outbox = run_dir / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        payload = post.model_dump()
        payload["note"] = "dry run: nothing was sent"
        (outbox / f"{self.platform}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "dry_run"
        return PublishResult(platform=self.platform, ok=True, status=status, url=str(outbox / f"{self.platform}.json"))


async def publish_posts(posts: dict[str, PlatformPost], run_dir: Path, platforms: list[str] | None = None) -> list[PublishResult]:
    """Publish each platform independently: one failure never stops the others."""
    results: list[PublishResult] = []
    for name, post in posts.items():
        if platforms is not None and name not in platforms:
            continue
        if post.errors:
            results.append(PublishResult(platform=name, ok=False, status="failed", error="; ".join(post.errors)))
            continue
        try:
            results.append(await DryRunPublisher(name).publish(post, run_dir))
        except Exception as e:  # pragma: no cover - defensive
            results.append(PublishResult(platform=name, ok=False, status="failed", error=str(e)))
    return results
