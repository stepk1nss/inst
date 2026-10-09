"""Build the final post for each platform from fields + platform rules."""

from __future__ import annotations

import html
import re
from pathlib import Path

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from ..config.schema import Project, Scenario
from ..media.image import pad_to_aspect
from ..publishers.capabilities import CAPS
from .context import public_fields
from .state import PlatformPost, RunState

_env_plain = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)
_env_html = SandboxedEnvironment(undefined=StrictUndefined, autoescape=True, keep_trailing_newline=False)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANKS_RE = re.compile(r"\n{3,}")


def visible_length(text: str, is_html: bool) -> int:
    return len(html.unescape(_TAG_RE.sub("", text))) if is_html else len(text)


def normalize_hashtag(tag: str) -> str:
    tag = re.sub(r"\s+", "", str(tag)).lstrip("#")
    return f"#{tag}" if tag else ""


def merge_hashtags(fixed: list[str], generated: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in [normalize_hashtag(t) for t in fixed] + [normalize_hashtag(t) for t in generated][:limit]:
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def compose_posts(project: Project, scenario: Scenario, state: RunState, run_dir: Path) -> dict[str, PlatformPost]:
    posts: dict[str, PlatformPost] = {}
    image = state.media[0].path if state.media else None
    source_video = state.source_video.path if state.source_video else None
    # video material: the voiced version if built, else the original clip
    video = state.artifacts.get("video") or source_video

    for pname, target in project.enabled_platforms().items():
        pd = scenario.platform_defaults[pname]
        caps = CAPS[pname]
        # the core decides visibility; a template cannot reach secrets it was not granted
        values = dict(state.fields) if pd.reveal_secrets else public_fields(scenario, state.fields)
        warnings: list[str] = []
        errors: list[str] = []
        title = None
        generated: list[str] = []

        if pd.template is not None:
            env = _env_html if caps.html else _env_plain
            text = env.from_string(pd.template).render(**values)
        else:
            base, _, sub = (pd.text_from or "").partition(".")
            value = values.get(base)
            if sub:
                entry = (value or {}).get(sub) or {}
                text = str(entry.get("text", ""))
                title = entry.get("title")
                generated = list(entry.get("hashtags") or [])
            else:
                text = "" if value is None else str(value)
            if caps.html:
                text = html.escape(text, quote=False)

        hashtags = merge_hashtags(target.hashtags_fixed, generated, caps.hashtags_max)
        text = _BLANKS_RE.sub("\n\n", text.strip())
        if hashtags:
            text = f"{text}\n\n{' '.join(hashtags)}"
        if pname in state.post_overrides:
            text = state.post_overrides[pname]
            warnings.append("текст изменён вручную")

        media_kind, media_path = None, None
        if source_video and video:  # the material is a video: every platform gets the video
            media_kind, media_path = "video", video
        elif target.wants_video:
            if video:
                media_kind, media_path = "video", video
            elif caps.requires_video:
                errors.append("нужно видео, но оно не собрано (нет озвучки или FFmpeg)")
            elif image:
                media_kind, media_path = "photo", image
                warnings.append("видео не собрано — будет опубликовано фото")
        elif image:
            media_kind, media_path = "photo", image

        if pname == "instagram" and media_kind == "photo" and media_path:
            padded = f"media/instagram-{Path(media_path).stem}.jpg"
            pad_to_aspect(run_dir / media_path, run_dir / padded)
            media_path = padded

        split = False
        if caps.caption_with_media_limit and media_kind:
            if visible_length(text, caps.html) > caps.caption_with_media_limit:
                split = True
                warnings.append(
                    f"текст длиннее {caps.caption_with_media_limit} символов — уйдёт отдельным сообщением после медиа"
                )

        posts[pname] = PlatformPost(
            platform=pname,
            connection=target.connection,
            format=target.format,
            media_kind=media_kind,
            media_path=media_path,
            text=text,
            title=title,
            hashtags=hashtags,
            reveal_secrets=pd.reveal_secrets,
            html=caps.html,
            split_caption=split,
            mode=target.mode,
            visibility=target.visibility,
            warnings=warnings,
            errors=errors,
        )
    return posts
