"""Static facts about each platform, shared by prompt building, compose and validation.

Numbers come from official docs (see docs/AUDIT.md, section 1). Values marked
"verify" were not confirmed against a live official page and should be
re-checked before relying on them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformCaps:
    name: str
    title: str
    text_limit: int  # max length of the main text/caption
    hashtags_max: int  # how many hashtags we generate (style choice, not an API limit)
    title_limit: int | None = None  # separate title field (YouTube)
    caption_with_media_limit: int | None = None  # Telegram: caption under a photo/video
    requires_video: bool = False
    html: bool = False  # text is sent with HTML parse mode
    prompt_rules: str = ""


CAPS: dict[str, PlatformCaps] = {
    "telegram": PlatformCaps(
        name="telegram",
        title="Telegram",
        text_limit=4096,
        caption_with_media_limit=1024,  # Bot API: caption 0-1024 chars after entity parsing
        hashtags_max=2,
        html=True,
        prompt_rules=(
            "Пост для Telegram-канала: можно длиннее и подробнее, абзацы, эмодзи умеренно, "
            "0–2 хэштега для навигации. Обычный текст без HTML-разметки."
        ),
    ),
    "instagram": PlatformCaps(
        name="instagram",
        title="Instagram",
        text_limit=2200,  # verify
        hashtags_max=5,
        prompt_rules=(
            "Подпись для Instagram: цепляющая первая строка, 2–4 коротких абзаца, "
            "вопрос к аудитории и призыв к действию; 3–5 точных хэштегов."
        ),
    ),
    "tiktok": PlatformCaps(
        name="tiktok",
        title="TikTok",
        text_limit=2200,  # verify (video caption); photo-post title is limited to 90
        hashtags_max=5,
        prompt_rules="Подпись для TikTok: очень коротко, хук + вопрос зрителю; 3–5 хэштегов.",
    ),
    "youtube": PlatformCaps(
        name="youtube",
        title="YouTube Shorts",
        text_limit=5000,
        title_limit=100,
        hashtags_max=5,  # YouTube ignores all hashtags if a video has more than 15
        requires_video=True,
        prompt_rules=(
            "YouTube Shorts: заголовок до 100 символов (цепляющий, без кликбейта-лжи), "
            "короткое описание 1–3 предложения; 3–5 хэштегов."
        ),
    ),
}
