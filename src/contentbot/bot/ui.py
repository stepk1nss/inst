"""Transport-neutral chat interface. The controller talks only to this;
the Telegram adapter, the console simulator and the tests implement it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # callback data, <= 64 bytes for Telegram


Keyboard = list[list[Button]]


class ChatUI(Protocol):
    async def send_text(self, chat_id: int, text: str, kb: Keyboard | None = None, html: bool = False) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str, kb: Keyboard | None = None, html: bool = False) -> None: ...

    async def send_photo(self, chat_id: int, path: Path, caption: str | None = None) -> int: ...

    async def send_video(self, chat_id: int, path: Path, caption: str | None = None) -> int: ...

    async def send_voice(self, chat_id: int, path: Path, caption: str | None = None) -> int: ...

    async def send_audio(self, chat_id: int, path: Path, caption: str | None = None) -> int: ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...
